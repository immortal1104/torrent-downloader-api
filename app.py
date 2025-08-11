import asyncio
import os
from collections import deque
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse

app = FastAPI()

DOWNLOAD_DIR = './downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
MAX_ACTIVE_DOWNLOADS = 2

download_queue = deque()
active_downloads = {}
completed_files = {}
downloading_tasks = {}
debug_logs = {}

def add_log(magnet, message):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {message}"
    debug_logs.setdefault(magnet, []).append(line)
    if len(debug_logs[magnet]) > 10:
        debug_logs[magnet] = debug_logs[magnet][-10:]

async def download_worker():
    while True:
        if len(active_downloads) < MAX_ACTIVE_DOWNLOADS and download_queue:
            magnet = download_queue.popleft()
            debug_logs[magnet] = []
            task = asyncio.create_task(handle_download(magnet))
            downloading_tasks[magnet] = task
        await asyncio.sleep(2)

async def handle_download(magnet):
    try:
        from torrentp import TorrentDownloader
    except ImportError:
        add_log(magnet, "ERROR: torrentp not installed")
        return

    try:
        torrent = TorrentDownloader(magnet, DOWNLOAD_DIR)
        
        # Initialize with safe defaults
        active_downloads[magnet] = {
            "status": "Connecting to peers...",
            "progress": "0.00%",
            "download_speed": "0 KB/s",
            "peers": 0,
            "eta": "Calculating...",
            "file_size": "Unknown",
            "logs": debug_logs.get(magnet, [])
        }
        
        add_log(magnet, "Initializing torrent download")

        await torrent.start_download()
        add_log(magnet, "Torrent client started")

        no_peer_start_time = None
        update_count = 0
        
        while torrent.status.is_downloading:
            update_count += 1
            
            # Safe extraction with defaults
            peers = getattr(torrent.status, 'num_peers', 0) or 0
            progress = getattr(torrent.status, 'progress', 0) or 0.0
            speed_bps = getattr(torrent.status, 'download_rate', 0) or 0
            total_size = getattr(torrent.status, 'total_size', 0) or 0

            add_log(magnet, f"Update #{update_count}: Peers={peers}, Progress={progress:.2f}%, Speed={speed_bps/1024:.1f}KB/s")

            # Handle no peers timeout
            if peers == 0:
                if no_peer_start_time is None:
                    no_peer_start_time = asyncio.get_running_loop().time()
                    add_log(magnet, "No peers detected - starting timeout timer")
                elif asyncio.get_running_loop().time() - no_peer_start_time > 120:
                    add_log(magnet, "Auto-cancelling: no peers for 2 minutes")
                    await torrent.stop_download()
                    active_downloads.pop(magnet, None)
                    downloading_tasks.pop(magnet, None)
                    return
            else:
                no_peer_start_time = None

            # Calculate ETA safely
            eta_str = "Calculating..."
            if speed_bps > 100 and progress > 0 and progress < 100 and total_size > 0:
                bytes_remaining = total_size * (100 - progress) / 100
                eta_seconds = int(bytes_remaining / speed_bps)
                h, remainder = divmod(eta_seconds, 3600)
                m, s = divmod(remainder, 60)
                if h > 0:
                    eta_str = f"{h:01d}:{m:02d}:{s:02d}"
                else:
                    eta_str = f"{m:02d}:{s:02d}"

            # Update with guaranteed safe values
            active_downloads[magnet] = {
                "status": "Downloading" if peers > 0 else "Waiting for peers...",
                "progress": f"{max(0, min(100, progress)):.2f}%",
                "download_speed": f"{speed_bps / 1024:.1f} KB/s" if speed_bps > 0 else "0 KB/s",
                "peers": max(0, peers),
                "eta": eta_str,
                "file_size": f"{total_size / (1024*1024):.1f} MB" if total_size > 0 else "Unknown",
                "logs": debug_logs.get(magnet, [])
            }
            
            await asyncio.sleep(3)

        # Handle completion
        if hasattr(torrent.status, 'is_finished') and torrent.status.is_finished:
            add_log(magnet, "Download completed successfully")
            active_downloads.pop(magnet, None)
            completed_files[magnet] = []
            
            if hasattr(torrent, 'files') and torrent.files:
                for file in torrent.files:
                    path = os.path.join(DOWNLOAD_DIR, file)
                    if os.path.exists(path):
                        completed_files[magnet].append({
                            "file": file,
                            "download_url": f"/file/{file}"
                        })
                        add_log(magnet, f"File ready: {file}")
            
        await torrent.stop_download()
        
    except Exception as e:
        add_log(magnet, f"ERROR: {str(e)}")
        active_downloads.pop(magnet, None)
    finally:
        downloading_tasks.pop(magnet, None)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(download_worker())

@app.get("/")
def home():
    return {"message": "Torrent Downloader API running (local only)"}

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
      <title>Torrent Dashboard</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
      <style>
        .progress { height: 24px; }
        .torrent { margin-bottom: 24px; }
        .log-box { 
          background: #1e1e1e; 
          color: #00ff00; 
          font-family: monospace;
          font-size: 11px; 
          padding: 8px; 
          border-radius: 4px; 
          max-height: 80px; 
          overflow-y: auto; 
          white-space: pre-wrap;
        }
        .magnet-text {
          word-break: break-all;
          font-size: 11px;
          max-height: 40px;
          overflow: hidden;
        }
      </style>
    </head>
    <body class="bg-light">
      <div class="container py-4">
        <h2>Add Torrent</h2>
        <form id="addForm" class="input-group mb-4">
          <input type="text" id="magnet" class="form-control" placeholder="Paste magnet link" required />
          <button class="btn btn-success" type="submit">Add Torrent</button>
        </form>
        <h2>Active Torrents (<span id="activeCount">0</span>)</h2>
        <div id="active"></div>
        <h2 class="mt-4">Completed Torrents (<span id="completedCount">0</span>)</h2>
        <div id="completed"></div>
      </div>
      <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
      <script>
        function refreshDashboard() {
          $.get("/progress", function(data){
            let activeHTML = '';
            let activeCount = 0;
            
            for(const mag in data.active){
              activeCount++;
              const info = data.active[mag];
              
              // Safe value extraction with fallbacks
              const progress = info.progress || "0.00%";
              const eta = info.eta || "Unknown";
              const fileSize = info.file_size || "Unknown"; 
              const peers = (info.peers !== undefined && info.peers !== null) ? info.peers : "N/A";
              const speed = info.download_speed || "0 KB/s";
              const status = info.status || "Unknown";
              
              const prog = parseFloat(progress) || 0;
              const logs = (info.logs || []).join('\\n');
              
              activeHTML += `
                <div class="torrent card shadow-sm mb-3">
                  <div class="card-body">
                    <div><strong>Status:</strong> ${status}</div>
                    <div class="magnet-text text-muted mb-2">${mag}</div>
                    <div class="row mb-2">
                      <div class="col-md-6"><strong>Progress:</strong> ${progress}</div>
                      <div class="col-md-6"><strong>ETA:</strong> ${eta}</div>
                    </div>
                    <div class="row mb-2">
                      <div class="col-md-4"><strong>Size:</strong> ${fileSize}</div>
                      <div class="col-md-4"><strong>Peers:</strong> ${peers}</div>
                      <div class="col-md-4"><strong>Speed:</strong> ${speed}</div>
                    </div>
                    <div class="progress my-2">
                      <div class="progress-bar bg-success progress-bar-striped progress-bar-animated" 
                           role="progressbar" style="width:${prog}%" aria-valuenow="${prog}" 
                           aria-valuemin="0" aria-valuemax="100">${progress}</div>
                    </div>
                    <div class="log-box mb-2">${logs}</div>
                    <button class="btn btn-danger btn-sm delete-btn" data-mag="${mag}">Cancel Download</button>
                  </div>
                </div>`;
            }
            $("#active").html(activeHTML || "<p class='text-muted'>No active downloads</p>");
            $("#activeCount").text(activeCount);

            let completedHTML = '';
            let completedCount = 0;
            for(const mag in data.completed){
              data.completed[mag].forEach(f => {
                completedCount++;
                const fileName = f.file || "Unknown file";
                const downloadUrl = f.download_url || "#";
                completedHTML += `
                  <div class="mb-2">
                    <a class="btn btn-outline-success btn-sm" href="${downloadUrl}" target="_blank">
                      📁 ${fileName}
                    </a>
                  </div>`;
              });
            }
            $("#completed").html(completedHTML || "<p class='text-muted'>No completed downloads</p>");
            $("#completedCount").text(completedCount);

            $('.delete-btn').off('click').on('click', function(){
              let mag = $(this).data('mag');
              $(this).prop('disabled', true).text('Cancelling...');
              $.ajax({
                type: "POST",
                url: "/delete",
                contentType: "application/json",
                data: JSON.stringify({magnet: mag}),
                success: function(){ 
                  refreshDashboard(); 
                },
                error: function(){
                  $(this).prop('disabled', false).text('Cancel Download');
                }
              });
            });
          }).fail(function(){
            $("#active").html("<p class='text-danger'>Failed to load progress data</p>");
          });
        }

        $("#addForm").submit(function(e){
          e.preventDefault();
          let mag = $("#magnet").val().trim();
          if(mag){
            $("button[type=submit]").prop('disabled', true).text('Adding...');
            $.ajax({
              type: "POST",
              url: "/add",
              contentType: "application/json",
              data: JSON.stringify({magnet: mag}),
              success: function(response){ 
                if(response.added) {
                  $("#magnet").val(''); 
                  refreshDashboard();
                }
                $("button[type=submit]").prop('disabled', false).text('Add Torrent');
              },
              error: function(){
                $("button[type=submit]").prop('disabled', false).text('Add Torrent');
                alert('Failed to add torrent');
              }
            });
          }
        });

        // Refresh every 4 seconds
        setInterval(refreshDashboard, 4000);
        refreshDashboard();
      </script>
    </body>
    </html>
    """

@app.post("/add")
async def add_torrent(request: Request):
    try:
        data = await request.json()
        magnet = data.get("magnet", "").strip()
        if magnet and magnet.startswith("magnet:") and magnet not in active_downloads and magnet not in download_queue:
            download_queue.append(magnet)
            return {"added": True, "message": "Torrent queued successfully"}
        return {"added": False, "message": "Invalid or duplicate magnet link"}
    except Exception as e:
        return {"added": False, "message": str(e)}

@app.post("/delete")
async def delete_torrent(request: Request):
    try:
        data = await request.json()
        magnet = data.get("magnet")
        if magnet in downloading_tasks:
            downloading_tasks[magnet].cancel()
            active_downloads.pop(magnet, None)
            downloading_tasks.pop(magnet, None)
            add_log(magnet, "Cancelled by user")
            return {"deleted": True, "message": "Download cancelled"}
        if magnet in download_queue:
            download_queue.remove(magnet)
            return {"deleted": True, "message": "Removed from queue"}
        return {"deleted": False, "message": "Torrent not found"}
    except Exception as e:
        return {"deleted": False, "message": str(e)}

@app.get("/progress")
def get_progress():
    return {
        "active": active_downloads,
        "completed": completed_files,
        "queue_length": len(download_queue)
    }

@app.get("/file/{filename}")
def serve_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(path):
        return FileResponse(path, filename=filename)
    return {"error": "File not found"}
    
