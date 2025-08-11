import asyncio
import os
from collections import deque
from fastapi import FastAPI, Request, Form
from fastapi.responses import PlainTextResponse, HTMLResponse, FileResponse
from torrentp import TorrentDownloader

app = FastAPI()

# ===== Torrent Downloader Config ===== #
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

# ===== Torrent Handler ===== #
async def handle_download(magnet):
    torrent = TorrentDownloader(magnet, DOWNLOAD_DIR)
    active_downloads[magnet] = {"status": "Connecting to peers..."}
    await torrent.start_download()

    no_peer_start_time = None
    while torrent.status.is_downloading:
        peers = torrent.status.num_peers
        progress = torrent.status.progress
        speed_bps = torrent.status.download_rate

        # Auto-cancel if no peers > 2 min
        if peers == 0:
            if no_peer_start_time is None:
                no_peer_start_time = asyncio.get_running_loop().time()
            elif asyncio.get_running_loop().time() - no_peer_start_time > 120:
                print(f"[AUTO-CANCEL] No peers for 2 minutes: {magnet}")
                await torrent.stop_download()
                active_downloads.pop(magnet, None)
                downloading_tasks.pop(magnet, None)
                return
        else:
            no_peer_start_time = None

        # ETA calculation
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
            path = os.path.join(DOWNLOAD_DIR, file)
            if os.path.exists(path):
                completed_files[magnet].append({
                    "file": file,
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

# ===== Dashboard ===== #
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
    <html>
    <head>
      <title>Torrent Dashboard</title>
      <style>
        body { font-family: Arial; margin: 20px; }
        .torrent { border: 1px solid #ccc; padding: 10px; margin-bottom: 15px; }
        .progress-container { background: #eee; height: 20px; border-radius: 5px; overflow:hidden; }
        .progress-bar { background: #4caf50; height: 100%; width: 0%; }
      </style>
    </head>
    <body>
      <h2>Active Downloads</h2>
      <div id="active"></div>
      <h2>Completed Downloads</h2>
      <div id="completed"></div>
      <script>
      async function loadProgress(){
        const r = await fetch('/progress');
        const data = await r.json();
        const c = document.getElementById('active');
        c.innerHTML='';
        for(const [magnet, info] of Object.entries(data.active_downloads)){
          let prog = parseFloat(info.progress)||0;
          c.innerHTML += `<div class="torrent">
              <div><b>Status:</b> ${info.status}</div>
              <div><b>Magnet:</b> ${magnet}</div>
              <div><b>Speed:</b> ${info.download_speed} |
                   <b>Peers:</b> ${info.peers} |
                   <b>ETA:</b> ${info.eta}</div>
              <div><b>Progress:</b> ${info.progress}</div>
              <div class="progress-container">
                  <div class="progress-bar" style="width:${prog}%;"></div>
              </div>
          </div>`;
        }
      }
      async function loadCompleted(){
        const r = await fetch('/completed');
        const data = await r.json();
        const c = document.getElementById('completed');
        c.innerHTML='';
        for(const [magnet, files] of Object.entries(data.completed_files)){
          files.forEach(f=>{
            c.innerHTML += `<div><a href="${f.download_url}" target="_blank">${f.file}</a></div>`;
          });
        }
      }
      async function refresh(){
        await loadProgress();
        await loadCompleted();
      }
      setInterval(refresh, 5000);
      refresh();
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
         
