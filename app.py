import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from torrentp import TorrentDownloader
import os
from collections import deque

app = FastAPI()

DOWNLOAD_DIR = './downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Config: Maximum simultaneous downloads
MAX_ACTIVE_DOWNLOADS = 2

# Data trackers
download_queue = deque()
active_downloads = {}
completed_files = {}
downloading_tasks = {}

# ---------------- Queue Worker ---------------- #
async def download_worker():
    """Background task that checks the queue and starts downloads."""
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

    while torrent.status.is_downloading:
        active_downloads[magnet] = {
            "status": "Downloading",
            "progress": f"{torrent.status.progress:.2f}%",
            "download_speed": f"{torrent.status.download_rate / 1024:.2f} KB/s",
            "peers": torrent.status.num_peers
        }
        await asyncio.sleep(2)

    if torrent.status.is_finished:
        active_downloads.pop(magnet, None)
        completed_files[magnet] = torrent.files
        print(f"Download finished: {torrent.files}")

    await torrent.stop_download()
    downloading_tasks.pop(magnet, None)

# ---------------- Startup ---------------- #
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(download_worker())

# ---------------- Endpoints ---------------- #
@app.get("/")
def home():
    return {"message": "Torrent Downloader API with Queue is running!"}

@app.post("/download")
async def download_torrent(request: Request):
    data = await request.json()
    magnet_link = data.get("magnet")

    if not magnet_link:
        return {"error": "Please provide magnet link in JSON format: {'magnet': 'link'}"}

    if magnet_link in active_downloads or magnet_link in download_queue:
        return {"status": "Already queued or downloading", "magnet": magnet_link}

    download_queue.append(magnet_link)
    return {"status": "Queued for download", "queue_position": len(download_queue), "magnet": magnet_link}

@app.get("/queue")
def get_queue():
    return {"queue": list(download_queue)}

@app.get("/progress")
def get_progress():
    return {"active_downloads": active_downloads}

@app.get("/completed")
def list_completed_files():
    result = []
    for magnet, files in completed_files.items():
        for file in files:
            file_path = os.path.join(DOWNLOAD_DIR, file)
            if os.path.exists(file_path):
                result.append({
                    "magnet": magnet,
                    "file": file,
                    "download_url": f"/file/{file}"
                })
    return {"completed_files": result}

@app.get("/file/{filename}")
def download_file(filename: str):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    return {"error": "File not found"}

# ---------------- Cancel Endpoint ---------------- #
@app.post("/cancel")
async def cancel_download(request: Request):
    data = await request.json()
    magnet_link = data.get("magnet")

    if not magnet_link:
        return {"error": "Please provide magnet link in JSON format: {'magnet': 'link'}"}

    # If currently downloading → cancel
    if magnet_link in downloading_tasks:
        downloading_tasks[magnet_link].cancel()
        active_downloads.pop(magnet_link, None)
        downloading_tasks.pop(magnet_link, None)
        return {"status": "Cancelled active download", "magnet": magnet_link}

    # If queued → remove it
    if magnet_link in download_queue:
        download_queue.remove(magnet_link)
        return {"status": "Removed from queue", "magnet": magnet_link}

    return {"status": "Not found", "magnet": magnet_link}
    
