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
        add_log(magnet, "ERROR: torrentp not installed.")
        return

    torrent = TorrentDownloader(magnet, DOWNLOAD_DIR)
    active_downloads[magnet] = {"status": "Connecting to peers..."}
    add_log(magnet, "Queued for download")

    await torrent.start_download()
    add_log(magnet, "Torrent client started")

    no_peer_start_time = None
    while torrent.status.is_downloading:
        peers = torrent.status.num_peers
        progress = torrent.status.progress
        speed_bps = torrent.status.download_rate or 0
        total_size = torrent.status.total_size or 0

        add_log(magnet, f"Peers={peers}, Progress={progress:.2f}%, Speed={speed_bps/1024:.2f}KB/s")
        if peers == 0:
            if no_peer_start_time is None:
                no_peer_start_time = asyncio.get_running_loop().time()
                add_log(magnet, "No peers, timer started")
            elif asyncio.get_running_loop().time() - no_peer_start_time > 120:
                add_log(magnet, "Auto-cancel: no peers for 2m")
                await torrent.stop_download()
                active_downloads.pop(magnet, None)
                downloading_tasks.pop(magnet, None)
                return
        else:
            no_peer_start_time = None

        eta_str = "Calculating..."
        if speed_bps > 0 and progress < 100 and total_size > 0:
            bytes_remaining = total_size * (100 - progress) / 100
            eta_seconds = bytes_remaining / speed_bps
            m, s = divmod(int(eta_seconds), 60)
            eta_str = f"{m:02d}:{s:02d}"

        active_downloads[magnet] = {
            "status": "Downloading",
            "progress": f"{progress:.2f}%",
            "download_speed": f"{speed_bps / 1024:.2f} KB/s",
            "peers": peers,
            "eta": eta_str,
            "file_size": f"{total_size / (1024*1024):.2f} MB" if total_size > 0 else "Unknown",
            "logs": debug_logs.get(magnet, [])
        }
        await asyncio.sleep(2)

    if torrent.status.is_finished:
        add_log(magnet, "Download complete")
        active_downloads.pop(magnet, None)
        completed_files[magnet] = []
        for file in torrent.files:
            path = os.path.join(DOWNLOAD_DIR, file)
            if os.path.exists(path):
                completed_files[magnet].append({
                    "file": file,
                    "download_url": f"/file/{file}"
                })
        await torrent.stop_download()
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
        .log-box { background: #20242a; color: #8af372; font-size: 12px; padding: 5px 10px; border-radius: 7px; max-height: 70px; overflow-y: auto; }
      </style>
    </head>
    <body class="bg-light">
      <div class="container py-4">
        <h2>Add Torrent</h2>
        <form id="addForm" class="input-group mb-4">
          <input type="text" id="magnet" class="form-control" placeholder="Paste magnet link" required />
          <button class="btn btn-success" type="submit">Add Torrent</button>
        </form>
        <h2>Active Torrents</h2>
        <div id="active"></div>
        <h2 class="mt-4">Completed Torrents</h2>
        <div id="completed"></div>
      </div>
      <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
      <script>
        function refreshDashboard() {
          $.get("/progress", function(data){
            let activeHTML = '';
            for(const mag in data.active){
              const info = data.active[mag];
              const prog = parseFloat(info.progress) || 0;
              const logs = (info.logs || []).join('<br>');
              activeHTML += `
                <div class="torrent card shadow-sm mb-3">
                  <div class="card-body">
                    <div><strong>Status:</strong> ${info.status}</div>
                    <div class="small text-muted mb-1">${mag}</div>
                    <div><strong>Progress:</strong> ${info.progress} &nbsp;|&nbsp; <strong>ETA:</strong> ${info.eta} &nbsp;|&nbsp; <strong>Size:</strong> ${info.file_size} &nbsp;|&nbsp; <strong>Peers:</strong> ${info.peers} &nbsp;|&nbsp; <strong>Speed:</strong> ${info.download_speed}</div>
                    <div class="progress my-2">
                      <div class="progress-bar bg-success progress-bar-striped progress-bar-animated" role="progressbar" style="width:${prog}%">${info.progress}</div>
                    </div>
                    <div class="log-box mb-2">${logs}</div>
                    <button class="btn btn-danger btn-sm delete-btn" data-mag="${mag}">Delete</button>
                  </div>
                </div>`;
            }
            $("#active").html(activeHTML || "<p>No active downloads</p>");

            let completedHTML = '';
            for(const mag in data.completed){
              data.completed[mag].forEach(f => {
                completedHTML += `<div class="mb-2"><a class="btn btn-outline-primary btn-sm" href="${f.download_url}" target="_blank">${f.file}</a></div>`;
              });
            }
            $("#completed").html(completedHTML || "<p>No completed downloads</p>");

            $('.delete-btn').click(function(){
              let mag = $(this).data('mag');
              $.ajax({
                type: "POST",
                url: "/delete",
                contentType: "application/json",
                data: JSON.stringify({magnet: mag}),
                success: function(){ refreshDashboard(); }
              });
            });
          });
        }

        $("#addForm").submit(function(e){
          e.preventDefault();
          let mag = $("#magnet").val();
          if(mag){
            $.ajax({
              type: "POST",
              url: "/add",
              contentType: "application/json",
              data: JSON.stringify({magnet: mag}),
              success: function(){ $("#magnet").val(''); refreshDashboard(); }
            });
          }
        });

        setInterval(refreshDashboard, 3000);
        refreshDashboard();
      </script>
    </body>
    </html>
    """

@app.post("/add")
async def add_torrent(request: Request):
    data = await request.json()
    magnet = data.get("magnet")
    if magnet and magnet not in active_downloads and magnet not in download_queue:
        download_queue.append(magnet)
        return {"added": True}
    return {"added": False}

@app.post("/delete")
async def delete_torrent(request: Request):
    data = await request.json()
    magnet = data.get("magnet")
    if magnet in downloading_tasks:
        downloading_tasks[magnet].cancel()
        active_downloads.pop(magnet, None)
        downloading_tasks.pop(magnet, None)
        debug_logs[magnet] = ["Cancelled by user."]
        return {"deleted": True}
    if magnet in download_queue:
        download_queue.remove(magnet)
        return {"deleted": True}
    return {"deleted": False}

@app.get("/progress")
def get_progress():
    return {
        "active": active_downloads,
        "completed": completed_files,
    }

@app.get("/file/{filename}")
def serve_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(path):
        return FileResponse(path, filename=filename)
    return {"error": "File not found"}
