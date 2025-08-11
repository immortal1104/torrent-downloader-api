import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse
from torrentp import TorrentDownloader
import os
from collections import deque

app = FastAPI()

# Folder to store downloads
DOWNLOAD_DIR = './downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Maximum simultaneous downloads
MAX_ACTIVE_DOWNLOADS = 2

# State trackers
download_queue = deque()
active_downloads = {}
completed_files = {}
downloading_tasks = {}

# ---------------- Background Worker ---------------- #
async def download_worker():
    while True:
        if len(active_downloads) < MAX_ACTIVE_DOWNLOADS and download_queue:
            magnet = download_queue.popleft()
            task = asyncio.create_task(handle_download(magnet))
            downloading_tasks[magnet] = task
        await asyncio.sleep(2)

# ---------------- Torrent Handler ---------------- #
async def handle_download(magnet):
    torrent = TorrentDownloader(magnet, DOWNLOAD_DIR)
    active_downloads[magnet] = {"status": "Connecting to peers..."}
    await torrent.start_download()

    no_peer_start_time = None

    while torrent.status.is_downloading:
        peers = torrent.status.num_peers
        progress = torrent.status.progress
        speed_bps = torrent.status.download_rate  # Bytes/sec

        # Auto-cancel after 2 minutes of no peers
        if peers == 0:
            if no_peer_start_time is None:
                no_peer_start_time = asyncio.get_running_loop().time()
            elif asyncio.get_running_loop().time() - no_peer_start_time > 120:
                print(f"[Auto-Cancel] No peers for 2 minutes: {magnet}")
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

    if torrent.status.is_finished:
        active_downloads.pop(magnet, None)
        completed_files[magnet] = torrent.files
        print(f"✅ Finished: {torrent.files}")

    await torrent.stop_download()
    downloading_tasks.pop(magnet, None)

# ---------------- Startup ---------------- #
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(download_worker())

# ---------------- API Endpoints ---------------- #
@app.get("/")
def home():
    return {"message": "Torrent Downloader API with Queue + Auto-Cancel + ETA + Text Output is running!"}

@app.post("/download")
async def download_torrent(request: Request):
    data = await request.json()
    magnet = data.get("magnet")
    if not magnet:
        return {"error": "Please provide magnet link: {'magnet': 'link'}"}
    if magnet in active_downloads or magnet in download_queue:
        return {"status": "Already queued or downloading", "magnet": magnet}
    download_queue.append(magnet)
    return {"status": "Queued", "queue_position": len(download_queue), "magnet": magnet}

@app.get("/queue")
def get_queue():
    return {"queue": list(download_queue)}

@app.get("/progress")
def get_progress():
    return {"active_downloads": active_downloads}

# New plain-text progress endpoint
@app.get("/progress-text", response_class=PlainTextResponse)
def get_progress_text():
    if not active_downloads:
        return "No active downloads"
    lines = []
    for magnet, info in active_downloads.items():
        lines.append(f"Magnet: {magnet}")
        for k, v in info.items():
            lines.append(f"{k.capitalize()}: {v}")
        lines.append("")  # Empty line between torrents
    return "\n".join(lines)

@app.get("/completed")
def list_completed():
    results = []
    for magnet, files in completed_files.items():
        for file in files:
            path = os.path.join(DOWNLOAD_DIR, file)
            if os.path.exists(path):
                results.append({
                    "magnet": magnet,
                    "file": file,
                    "download_url": f"/file/{file}"
                })
    return {"completed_files": results}

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
        return {"error": "Please provide magnet link: {'magnet': 'link'}"}

    if magnet in downloading_tasks:
        downloading_tasks[magnet].cancel()
        active_downloads.pop(magnet, None)
        downloading_tasks.pop(magnet, None)
        return {"status": "Cancelled active download", "magnet": magnet}

    if magnet in download_queue:
        download_queue.remove(magnet)
        return {"status": "Removed from queue", "magnet": magnet}

    return {"status": "Not found", "magnet": magnet}
