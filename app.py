import asyncio
import os
import time
from collections import deque
from pathlib import Path
from fastapi import FastAPI, Request, Form
from fastapi.responses import PlainTextResponse, HTMLResponse, FileResponse, Response
from torrentp import TorrentDownloader

app = FastAPI()

# ===== Config ===== #
DOWNLOAD_DIR = './downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
MAX_ACTIVE_DOWNLOADS = 2

download_queue = deque()
active_downloads = {}
completed_files = {}
downloading_tasks = {}

# ===== Background Worker ===== #
async def download_worker():
    while True:
        if len(active_downloads) < MAX_ACTIVE_DOWNLOADS and download_queue:
            magnet = download_queue.popleft()
            task = asyncio.create_task(handle_download(magnet))
            downloading_tasks[magnet] = task
        await asyncio.sleep(2)

# ===== Download Handler ===== #
async def handle_download(magnet):
    torrent = TorrentDownloader(magnet, DOWNLOAD_DIR)
    active_downloads[magnet] = {"status": "Connecting to peers..."}
    await torrent.start_download()

    no_peer_start_time = None
    start_time = time.time()

    while not torrent.status.is_finished:
        peers = torrent.status.num_peers or 0
        progress = torrent.status.progress or 0.0
        speed_bps = torrent.status.download_rate or 0

        # Auto-cancel if no peers for > 2 min
        if peers == 0:
            if no_peer_start_time is None:
                no_peer_start_time = time.time()
            elif time.time() - no_peer_start_time > 120:
                print(f"[AUTO-CANCEL] No peers for 2 minutes: {magnet}")
                await torrent.stop_download()
                active_downloads.pop(magnet, None)
                downloading_tasks.pop(magnet, None)
                return
        else:
            no_peer_start_time = None

        # ETA
        eta_str = "Calculating..."
        if speed_bps > 0 and progress < 100.0:
            bytes_remaining = (torrent.status.total_size * (100 - progress)) / 100
            eta_seconds = bytes_remaining / speed_bps
            m, s = divmod(int(eta_seconds), 60)
            eta_str = f"{m:02d}:{s:02d}"

        active_downloads[magnet] = {
            "status": "Downloading",
            "progress": f"{progress:.2f}%",
            "download_speed": f"{speed_bps / 1024:.2f} KB/s",
            "peers": peers,
            "eta": eta_str
        }
        await asyncio.sleep(2)

    # On completion
    if torrent.status.is_finished:
        active_downloads.pop(magnet, None)
        completed_files[magnet] = []
        for file in torrent.files:
            path = Path(DOWNLOAD_DIR) / file
            if path.exists():
                completed_files[magnet].append({
                    "file": file,
                    "size": f"{path.stat().st_size / (1024*1024):.2f} MB",
                    "finished_at": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time())),
                    "download_url": f"/file/{file}"
                })
        print(f"✅ Finished: {torrent.files}")

    await torrent.stop_download()
    downloading_tasks.pop(magnet, None)

# ===== Startup ===== #
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(download_worker())

# ===== HTML Form ===== #
@app.get("/add-magnet", response_class=HTMLResponse)
def add_magnet_form():
    return """
    <html><head><title>Add Magnet</title></head>
    <body style="font-family:Arial">
      <h2>Add Magnet Link</h2>
      <form action="/download" method="post">
        <input type="text" name="magnet" size="80" placeholder="Paste magnet link here" required>
        <br><br>
        <input type="submit" value="Add to Queue">
      </form>
      <p><a href="/dashboard" target="_blank">View Dashboard</a></p>
    </body></html>
    """

# ===== Bootstrap Dashboard ===== #
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
    <html>
    <head>
      <title>Torrent Dashboard</title>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light">
      <div class="container my-4">
        <h1 class="mb-4 text-center">📥 Torrent Dashboard</h1>

        <!-- Active Downloads -->
        <div class="card mb-4 shadow-sm">
          <div class="card-header bg-primary text-white">Active Downloads</div>
          <div class="card-body" id="active">
            <p class="text-muted">Loading...</p>
          </div>
        </div>

        <!-- Queue -->
        <div class="card mb-4 shadow-sm">
          <div class="card-header bg-warning">Queue</div>
          <div class="card-body" id="queue">
            <p class="text-muted">Loading...</p>
          </div>
        </div>

        <!-- Completed Downloads -->
        <div class="card mb-4 shadow-sm">
          <div class="card-header bg-success text-white">Completed Downloads</div>
          <div class="card-body" id="completed">
            <p class="text-muted">Loading...</p>
          </div>
        </div>
      </div>

      <script>
      async function loadProgress(){
        const r = await fetch('/progress');
        const data = await r.json();
        const c = document.getElementById('active');
        c.innerHTML = '';
        for (const [magnet, info] of Object.entries(data.active_downloads)) {
          const prog = info.progress ? parseFloat(info.progress) : 0;
          const badgeClass = info.status.includes("Downloading") ? "bg-info" : "bg-secondary";
          c.innerHTML += `
            <div class="mb-3">
              <h6><span class="badge ${badgeClass}">${info.status}</span></h6>
              <div class="small text-break"><b>Magnet:</b> ${magnet}</div>
              <div class="small"><b>Speed:</b> ${info.download_speed} | <b>Peers:</b> ${info.peers} | <b>ETA:</b> ${info.eta}</div>
              <div class="progress mt-2">
                <div class="progress-bar progress-bar-striped progress-bar-animated" role="progressbar" style="width:${prog}%">
                  ${info.progress}
                </div>
              </div>
            </div>
          `;
        }
        if (Object.keys(data.active_downloads).length === 0) {
          c.innerHTML = "<p class='text-muted'>No active downloads</p>";
        }
      }

      async function loadQueue(){
        const r = await fetch('/queue');
        const data = await r.json();
        const c = document.getElementById('queue');
        if (data.queue.length > 0) {
          c.innerHTML = '<ul class="list-group">';
          data.queue.forEach((m, i) => {
            c.innerHTML += `<li class="list-group-item"><b>${i+1}.</b> ${m}</li>`;
          });
          c.innerHTML += '</ul>';
        } else {
          c.innerHTML = "<p class='text-muted'>Queue is empty</p>";
        }
      }

      async function loadCompleted(){
        const r = await fetch('/completed');
        const data = await r.json();
        const c = document.getElementById('completed');
        let html = '';
        let hasFiles = false;
        for (const [magnet, files] of Object.entries(data.completed_files)) {
          files.forEach(f => {
            hasFiles = true;
            html += `<div class="mb-2">
              <a href="${f.download_url}" target="_blank" class="text-decoration-none">${f.file}</a>
              <span class="badge bg-secondary">${f.size}</span>
              <small class="text-muted">Finished at ${f.finished_at}</small>
            </div>`;
          });
        }
        c.innerHTML = hasFiles ? html : "<p class='text-muted'>No completed downloads</p>";
      }

      async function refresh(){
        await loadProgress();
        await loadQueue();
        await loadCompleted();
      }
      setInterval(refresh, 2000); refresh();
      </script>
    </body>
    </html>
    """

# ===== API Endpoints ===== #
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
    if magnet in active_downloads or magnet in download_queue or magnet in completed_files:
        return {"status": "Already queued/downloading/completed"}
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
    path = Path(DOWNLOAD_DIR) / filename
    if path.exists() and path.is_file() and path.resolve().parent == Path(DOWNLOAD_DIR).resolve():
        return FileResponse(str(path), filename=filename)
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

# ===== Flash crossdomain.xml Support ===== #
@app.get("/crossdomain.xml")
def crossdomain():
    xml = """<?xml version="1.0"?>
    <!DOCTYPE cross-domain-policy SYSTEM "http://www.macromedia.com/xml/dtds/cross-domain-policy.dtd">
    <cross-domain-policy>
        <allow-access-from domain="*" secure="false"/>
    </cross-domain-policy>
    """
    return Response(content=xml, media_type="application/xml")
