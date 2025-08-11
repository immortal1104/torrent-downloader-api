import asyncio
import os
from collections import deque
from fastapi import FastAPI, Request, Form
from fastapi.responses import PlainTextResponse, HTMLResponse, FileResponse, Response
from torrentp import TorrentDownloader

app = FastAPI()

DOWNLOAD_DIR = './downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
MAX_ACTIVE_DOWNLOADS = 2

download_queue = deque()
active_downloads = {}
completed_files = {}
downloading_tasks = {}

# Background worker to handle queued torrents
async def download_worker():
    while True:
        if len(active_downloads) < MAX_ACTIVE_DOWNLOADS and download_queue:
            magnet = download_queue.popleft()
            task = asyncio.create_task(handle_download(magnet))
            downloading_tasks[magnet] = task
        await asyncio.sleep(2)

# Torrent download handler with progress tracking and auto-cancel
async def handle_download(magnet):
    torrent = TorrentDownloader(magnet, DOWNLOAD_DIR)
    active_downloads[magnet] = {"status": "Connecting to peers..."}
    await torrent.start_download()

    no_peer_start_time = None
    while torrent.status.is_downloading:
        peers = torrent.status.num_peers
        progress = torrent.status.progress
        speed_bps = torrent.status.download_rate or 0
        total_size = torrent.status.total_size or 0

        # Auto-cancel after 2 minutes no peers
        if peers == 0:
            if no_peer_start_time is None:
                no_peer_start_time = asyncio.get_running_loop().time()
            elif asyncio.get_running_loop().time() - no_peer_start_time > 120:
                await torrent.stop_download()
                active_downloads.pop(magnet, None)
                downloading_tasks.pop(magnet, None)
                return
        else:
            no_peer_start_time = None

        # ETA calculation
        eta_str = "Calculating..."
        if speed_bps > 0 and progress < 100:
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
            "file_size": f"{total_size / (1024*1024):.2f} MB" if total_size > 0 else "Unknown"
        }
        await asyncio.sleep(2)

    if torrent.status.is_finished:
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

# Bootstrap-based form to add magnet links
@app.get("/add-magnet", response_class=HTMLResponse)
def add_magnet_form():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <title>Add Magnet Link</title>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="p-4">
      <div class="container">
        <h2 class="mb-3">Add Magnet Link</h2>
        <form action="/download" method="post" class="d-flex gap-2">
          <input type="text" name="magnet" class="form-control" placeholder="Paste magnet link" required />
          <button class="btn btn-primary" type="submit">Add to Queue</button>
        </form>
        <hr />
        <a href="/dashboard" class="btn btn-secondary mt-3">Go to Dashboard</a>
      </div>
      <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

# Bootstrap-powered real-time dashboard with progress bars and completed torrents
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <title>Torrent Dashboard</title>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet" />
      <style>
        .progress { height: 25px; }
        .torrent { margin-bottom: 20px; }
      </style>
    </head>
    <body class="p-4">
      <div class="container">
        <h2>Active Downloads</h2>
        <div id="active-downloads"></div>
        <h2 class="mt-5">Completed Downloads</h2>
        <div id="completed-downloads"></div>
      </div>

      <script>
        async function fetchAndRender() {
          try {
            const progressResp = await fetch('/progress');
            const progressData = await progressResp.json();
            const completedResp = await fetch('/completed');
            const completedData = await completedResp.json();

            const activeContainer = document.getElementById('active-downloads');
            const completedContainer = document.getElementById('completed-downloads');

            activeContainer.innerHTML = '';
            for (const [magnet, info] of Object.entries(progressData.active_downloads)) {
              const progressPercent = parseFloat(info.progress) || 0;

              const torrentDiv = document.createElement('div');
              torrentDiv.className = 'torrent border rounded p-3';

              torrentDiv.innerHTML = `
                <h5>Magnet:</h5><p class="text-break">${magnet}</p>
                <div class="mb-2">
                  <span><strong>Status:</strong> ${info.status || 'Unknown'}</span><br />
                  <span><strong>Progress:</strong> ${info.progress || '0%'}</span><br />
                  <span><strong>Speed:</strong> ${info.download_speed || '0 KB/s'}</span><br />
                  <span><strong>Peers:</strong> ${info.peers ?? 'N/A'}</span><br />
                  <span><strong>ETA:</strong> ${info.eta || 'Calculating...'}</span><br />
                  <span><strong>File Size:</strong> ${info.file_size || 'Unknown'}</span>
                </div>
                <div class="progress">
                  <div class="progress-bar progress-bar-striped progress-bar-animated bg-success" role="progressbar" aria-valuenow="${progressPercent}" aria-valuemin="0" aria-valuemax="100" style="width: ${progressPercent}%;"></div>
                </div>
              `;
              activeContainer.appendChild(torrentDiv);
            }
            if (Object.keys(progressData.active_downloads).length === 0) {
              activeContainer.innerHTML = '<p>No active downloads</p>';
            }

            completedContainer.innerHTML = '';
            let hasCompleted = false;
            for (const [magnet, files] of Object.entries(completedData.completed_files)) {
              files.forEach(f => {
                hasCompleted = true;
                const fileLink = document.createElement('a');
                fileLink.href = f.download_url;
                fileLink.target = '_blank';
                fileLink.textContent = f.file;
                const div = document.createElement('div');
                div.appendChild(fileLink);
                completedContainer.appendChild(div);
              });
            }
            if (!hasCompleted) {
              completedContainer.innerHTML = '<p>No completed downloads</p>';
            }
          } catch (e) {
            console.error('Error updating dashboard:', e);
          }
        }

        fetchAndRender();
        setInterval(fetchAndRender, 5000);
      </script>
      <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """

@app.get("/")
def home():
    return {"message": "Torrent Downloader API running (Local storage only)"}

@app.post("/download")
async def download_torrent(request: Request, magnet: str = Form(None)):
    if not magnet:
        try:
            data = await request.json()
            magnet = data.get("magnet")
        except:
            return {"error": "Provide magnet in form or JSON"}
    if not magnet:
        return {"error": "No magnet link provided"}
    if magnet in active_downloads or magnet in download_queue:
        return {"status": "Already queued/downloading"}
    download_queue.append(magnet)
    return {"status": "Queued", "queue_position": len(download_queue)}

@app.get("/queue")
def get_queue():
    return {"queue": list(download_queue)}

@app.get("/progress")
def get_progress():
    return {"active_downloads": active_downloads}

@app.get("/progress-text", response_class=PlainTextResponse)
def get_progress_text():
    if not active_downloads:
        return "No active downloads"
    lines = []
    for magnet, info in active_downloads.items():
        lines.append(f"Magnet: {magnet}")
        for k, v in info.items():
            lines.append(f"{k.capitalize()}: {v}")
        lines.append("")
    return "\n".join(lines)

@app.get("/completed")
def list_completed():
    return {"completed_files": completed_files}

@app.get("/file/{filename}")
def download_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(path):
        return FileResponse(path, filename=filename)
    return {"error": "File not found"}

@app.post("/cancel")
async def cancel_download(request: Request):
    data = await request.json()
    magnet = data.get("magnet")
    if not magnet:
        return {"error": "Provide {'magnet':'link'}"}
    if magnet in downloading_tasks:
        downloading_tasks[magnet].cancel()
        active_downloads.pop(magnet, None)
        downloading_tasks.pop(magnet, None)
        return {"status": "Cancelled active download"}
    if magnet in download_queue:
        download_queue.remove(magnet)
        return {"status": "Removed from queue"}
    return {"status": "Not found"}

@app.get("/crossdomain.xml")
def crossdomain():
    xml = """<?xml version="1.0"?>
    <!DOCTYPE cross-domain-policy SYSTEM "http://www.macromedia.com/xml/dtds/cross-domain-policy.dtd">
    <cross-domain-policy>
        <allow-access-from domain="*" secure="false"/>
    </cross-domain-policy>
    """
    return Response(content=xml, media_type="application/xml")
    
