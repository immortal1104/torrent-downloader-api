import asyncio
import os
from collections import deque
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse, Response
from torrentp import TorrentDownloader

# Optional Google Drive imports (not active now)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

app = FastAPI()

DOWNLOAD_DIR = './downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
MAX_ACTIVE_DOWNLOADS = 2

download_queue = deque()
active_downloads = {}
completed_files = {}
downloading_tasks = {}
debug_logs = {}  # magnet -> list of log lines


def add_log(magnet, message):
    """Keep last 10 debug log entries per torrent."""
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
    torrent = TorrentDownloader(magnet, DOWNLOAD_DIR)
    active_downloads[magnet] = {"status": "Connecting to peers..."}
    add_log(magnet, "Starting download task")

    await torrent.start_download()
    add_log(magnet, "Torrent client started")

    no_peer_start_time = None
    while torrent.status.is_downloading:
        peers = torrent.status.num_peers
        progress = torrent.status.progress
        speed_bps = torrent.status.download_rate or 0
        total_size = torrent.status.total_size or 0

        add_log(magnet,
                f"Peers: {peers}, Progress: {progress:.2f}%, Speed: {speed_bps/1024:.2f} KB/s")

        # Auto-cancel if no peers for > 2 min
        if peers == 0:
            if no_peer_start_time is None:
                no_peer_start_time = asyncio.get_running_loop().time()
                add_log(magnet, "No peers found. Timer started.")
            elif asyncio.get_running_loop().time() - no_peer_start_time > 120:
                add_log(magnet, "Auto-cancelling due to no peers for 2 min")
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


@app.get("/add-magnet", response_class=HTMLResponse)
def add_magnet_form():
    return """
    <!DOCTYPE html>
    <html>
    <head>
      <title>Add Magnet</title>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="p-4">
      <div class="container">
        <h2>Add Torrent Magnet Link</h2>
        <form action="/download" method="post" class="d-flex gap-2">
          <input type="text" name="magnet" class="form-control" placeholder="Paste magnet link here" required>
          <button class="btn btn-primary" type="submit">Add</button>
        </form>
        <hr>
        <a href="/dashboard" class="btn btn-secondary">View Dashboard</a>
      </div>
    </body>
    </html>
    """


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
      <title>Torrent Dashboard</title>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet" />
      <style>
        .torrent { border: 1px solid #ccc; padding: 15px; margin-bottom: 20px; border-radius: 8px; }
        .progress { height: 20px; }
        .log-box { background: #111; color: #0f0; padding: 5px; font-family: monospace; font-size: 12px; height: 100px; overflow-y: auto; }
      </style>
    </head>
    <body class="p-4">
      <div class="container">
        <h2>Active Downloads</h2>
        <div id="active"></div>
        <h2 class="mt-5">Completed Downloads</h2>
        <div id="completed"></div>
      </div>
      <script>
        async function refresh(){
          const progressResp = await fetch('/progress');
          const progressData = await progressResp.json();
          const completedResp = await fetch('/completed');
          const completedData = await completedResp.json();
          const activeElem = document.getElementById('active');
          const compElem = document.getElementById('completed');

          activeElem.innerHTML = '';
          for (const [magnet, info] of Object.entries(progressData.active_downloads)) {
            const prog = parseFloat(info.progress) || 0;
            const logs = info.logs ? info.logs.join('<br>') : '';
            activeElem.innerHTML += `
              <div class="torrent">
                <p><strong>Status:</strong> ${info.status}</p>
                <p><strong>Magnet:</strong> ${magnet}</p>
                <p><strong>Speed:</strong> ${info.download_speed} | <strong>Peers:</strong> ${info.peers} | <strong>ETA:</strong> ${info.eta} | <strong>Size:</strong> ${info.file_size}</p>
                <div class="progress">
                  <div class="progress-bar progress-bar-striped progress-bar-animated" style="width: ${prog}%" aria-valuenow="${prog}" aria-valuemin="0" aria-valuemax="100"></div>
                </div>
                <div class="log-box mt-2">${logs}</div>
              </div>
            `;
          }
          if (!Object.keys(progressData.active_downloads).length) activeElem.innerHTML = '<p>No active downloads</p>';

          compElem.innerHTML = '';
          for (const files of Object.values(completedData.completed_files)) {
            files.forEach(f => {
              compElem.innerHTML += `<div><a href="${f.download_url}" target="_blank">${f.file}</a></div>`;
            });
          }
          if (!Object.keys(completedData.completed_files).length) compElem.innerHTML = '<p>No completed downloads</p>';
        }
        setInterval(refresh, 3000);
        refresh();
      </script>
    </body>
    </html>
    """


@app.get("/")
def home():
    return {"message": "Torrent Downloader API running (Local only)"}


@app.post("/download")
async def download_torrent(request: Request, magnet: str = Form(None)):
    if not magnet:
        try:
            data = await request.json()
            magnet = data.get("magnet")
        except:
            return {"error": "Provide magnet in form or JSON"}
    if not magnet:
        return {"error": "No magnet provided"}
    if magnet in active_downloads or magnet in download_queue:
        return {"status": "Already queued/downloading"}
    download_queue.append(magnet)
    return {"status": "Queued", "queue_position": len(download_queue)}


@app.get("/progress")
def get_progress():
    return {"active_downloads": active_downloads}


@app.get("/completed")
def get_completed():
    return {"completed_files": completed_files}


@app.get("/file/{filename}")
def serve_file(filename: str):
    path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(path):
        return FileResponse(path, filename=filename)
    return {"error": "File not found"}


@app.post("/cancel")
async def cancel(request: Request):
    data = await request.json()
    magnet = data.get("magnet")
    if magnet in downloading_tasks:
        downloading_tasks[magnet].cancel()
        active_downloads.pop(magnet, None)
        downloading_tasks.pop(magnet, None)
        add_log(magnet, "Cancelled by user")
        return {"status": "Cancelled"}
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
    
