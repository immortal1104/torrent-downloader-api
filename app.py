import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from torrentp import TorrentDownloader
import os
from collections import deque

app = FastAPI()

DOWNLOAD_DIR = './downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Configuration
MAX_ACTIVE_DOWNLOADS = 2

# Queues and status tracking
download_queue = deque()
active_downloads = {}
completed_files = {}
downloading_tasks = {}

async def download_worker():
    """Background worker to manage download queue."""
    while True:
        # If we have space for more active downloads
        if len(active_downloads) < MAX_ACTIVE_DOWNLOADS and download_queue:
            magnet = download_queue.popleft()
            task = asyncio.create_task(handle_download(magnet))
            downloading_tasks[magnet] = task
        
        await asyncio.sleep(2)  # Prevent CPU overuse

async def handle_download(magnet):
    torrent = TorrentDownloader(magnet, DOWNLOAD_DIR)
    active_downloads[magnet] = "Starting..."
    await torrent.start_download()

    while torrent.status.is_downloading:
        active_downloads[magnet] = f"{torrent.status.progress:.2f}%"
        await asyncio.sleep(2)

    if torrent.status.is_finished:
        active_downloads.pop(magnet, None)
        completed_files[magnet] = torrent.files
        print(f"Finished: {torrent.files}")

    await torrent.stop_download()
    downloading_tasks.pop(magnet, None)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(download_worker())

@app.get("/")
def home():
    return {"message": "Torrent Downloader API with Queue is running!"}

@app.post("/download")
async def download_torrent(request: Request):
    data = await request.json()
    magnet_link = data.get("magnet")

    if not magnet_link:
        return {"error": "Please provide a magnet link in JSON format: {'magnet': 'your_link'}"}

    # Avoid duplicates
    if magnet_link in active_downloads or magnet_link in [m for m in download_queue]:
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
                result.append({"magnet": magnet, "file": file, "download_url": f"/file/{file}"})
    return {"completed_files": result}

@app.get("/file/{filename}")
def download_file(filename: str):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    return {"error": "File not found"}
