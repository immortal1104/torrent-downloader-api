import asyncio
import os
import json
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse
from torrentp import TorrentDownloader
from collections import deque
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = FastAPI()

# ================= Google Drive Setup (from ENV variable) ================= #
# Make sure in Render, you created an env var: SERVICE_ACCOUNT_JSON
service_account_info = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
credentials = service_account.Credentials.from_service_account_info(service_account_info)
drive_service = build('drive', 'v3', credentials=credentials)

def upload_to_drive(file_path, file_name):
    """Uploads a file to Google Drive and returns its file ID."""
    file_metadata = {'name': file_name}
    media = MediaFileUpload(file_path, resumable=True)
    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id'
    ).execute()
    return uploaded_file.get('id')

# ================= Torrent Downloader Config ================= #
DOWNLOAD_DIR = './downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_ACTIVE_DOWNLOADS = 2
download_queue = deque()
active_downloads = {}
completed_files = {}
downloading_tasks = {}

# ================= Background Worker ================= #
async def download_worker():
    while True:
        if len(active_downloads) < MAX_ACTIVE_DOWNLOADS and download_queue:
            magnet = download_queue.popleft()
            task = asyncio.create_task(handle_download(magnet))
            downloading_tasks[magnet] = task
        await asyncio.sleep(2)

# ================= Torrent Handler ================= #
async def handle_download(magnet):
    torrent = TorrentDownloader(magnet, DOWNLOAD_DIR)
    active_downloads[magnet] = {"status": "Connecting to peers..."}
    await torrent.start_download()

    no_peer_start_time = None

    while torrent.status.is_downloading:
        peers = torrent.status.num_peers
        progress = torrent.status.progress
        speed_bps = torrent.status.download_rate

        # Auto-cancel if no peers for > 2 mins
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

    # If download completes
    if torrent.status.is_finished:
        active_downloads.pop(magnet, None)
        completed_files[magnet] = []

        for file in torrent.files:
            local_path = os.path.join(DOWNLOAD_DIR, file)
            if os.path.exists(local_path):
                # Upload to Google Drive
                file_id = upload_to_drive(local_path, file)
                completed_files[magnet].append({
                    "file": file,
                    "drive_id": file_id,
                    "drive_link": f"https://drive.google.com/file/d/{file_id}/view"
                })
                print(f"✅ Uploaded {file} to Google Drive: {file_id}")
                os.remove(local_path)  # Delete after upload

        print(f"✅ Download finished & uploaded: {torrent.files}")

    await torrent.stop_download()
    downloading_tasks.pop(magnet, None)

# ================= Startup ================= #
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(download_worker())

# ================= API Endpoints ================= #
@app.get("/")
def home():
    return {"message": "Torrent Downloader API with Google Drive Upload, ETA, Auto-Cancel running!"}

@app.post("/download")
async def download_torrent(request: Request):
    data = await request.json()
    magnet = data.get("magnet")
    if not magnet:
        return {"error": "Please provide magnet link"}
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

@app.post("/cancel")
async def cancel_download(request: Request):
    data = await request.json()
    magnet = data.get("magnet")
    if not magnet:
        return {"error": "Please provide magnet link"}

    if magnet in downloading_tasks:
        downloading_tasks[magnet].cancel()
        active_downloads.pop(magnet, None)
        downloading_tasks.pop(magnet, None)
        return {"status": "Cancelled active download", "magnet": magnet}

    if magnet in download_queue:
        download_queue.remove(magnet)
        return {"status": "Removed from queue", "magnet": magnet}

    return {"status": "Not found", "magnet": magnet}
