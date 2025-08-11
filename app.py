# app.py
import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

# ---- torrentp import ----
# Make sure torrentp is installed in your environment.
from torrentp import TorrentDownloader

# ========== CONFIG ==========
# On Render attach a persistent disk and set DOWNLOAD_DIR to its mount point (e.g. /downloads).
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads")).resolve()
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DOWNLOAD_DIR / "state.json"
MAX_ACTIVE_DOWNLOADS = int(os.environ.get("MAX_ACTIVE_DOWNLOADS", 2))
PEER_TIMEOUT_SECONDS = int(os.environ.get("PEER_TIMEOUT_SECONDS", 120))

# ========== GLOBAL STATE ==========
app = FastAPI()
queue = deque()                 # magnets waiting
active_downloads: Dict[str, Any] = {}   # magnet -> TorrentDownloader instance (or metadata)
completed_files = []            # list of {filename, size, completed_at, magnet}
state_lock = asyncio.Lock()     # protects state file & in-memory structures

# helper: read/write state.json (queue + completed)
async def load_state():
    global queue, completed_files
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            q = data.get("queue", [])
            comp = data.get("completed", [])
            queue = deque(q)
            completed_files = comp
            print(f"[STATE] Loaded queue {len(q)} items and {len(comp)} completed entries")
        except Exception as e:
            print(f"[STATE] Failed to load state.json: {e}")

async def save_state():
    async with state_lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        payload = {"queue": list(queue), "completed": completed_files}
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except Exception as e:
            print(f"[STATE] Failed saving state: {e}")

# small helper to call start/stop which may be sync or async in torrentp
async def maybe_await(fn, *args, **kwargs):
    try:
        # try coroutine first
        coro = fn(*args, **kwargs)
        if asyncio.iscoroutine(coro):
            return await coro
        # else run in thread
        return await asyncio.to_thread(fn, *args, **kwargs)
    except TypeError:
        # fallback - call in thread
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception:
        # re-raise to be handled by caller
        raise

# ========= DOWNLOAD WORKER ==========
async def download_worker():
    while True:
        # Start as many as allowed
        while len(active_downloads) < MAX_ACTIVE_DOWNLOADS and queue:
            magnet = queue.popleft()
            # schedule worker
            task = asyncio.create_task(handle_download(magnet))
            # store a placeholder so dashboard shows it's active immediately
            active_downloads[magnet] = {"status": "Queued -> Starting", "task": task}
            await save_state()
        await asyncio.sleep(1)

async def handle_download(magnet: str):
    """
    Create TorrentDownloader, start downloading, update active_downloads dict,
    persist completed files.
    """
    try:
        torrent = TorrentDownloader(magnet, str(DOWNLOAD_DIR))
    except Exception as e:
        print(f"[ERROR] Creating TorrentDownloader for {magnet}: {e}")
        active_downloads.pop(magnet, None)
        return

    # persist the torrent instance and metadata
    active_downloads[magnet] = {"status": "Connecting to peers...", "torrent": torrent}

    try:
        # Try to start download (works whether start_download is async or sync)
        try:
            await maybe_await(torrent.start_download)
        except Exception as e:
            # If starting fails, mark error and return
            print(f"[ERROR] start_download failed for {magnet}: {e}")
            active_downloads[magnet]["status"] = f"Error starting: {e}"
            await asyncio.sleep(3)
            active_downloads.pop(magnet, None)
            return

        # Monitor peer availability & progress
        no_peer_start = None
        start_ts = time.time()

        # Ensure torrent.status attributes exist, poll until finished
        while True:
            st = getattr(torrent, "status", None)
            if st is None:
                # If status not available yet, wait a bit
                active_downloads[magnet]["status"] = "Initializing..."
                await asyncio.sleep(1)
                continue

            # read status safely (use getattr default values)
            is_downloading = getattr(st, "is_downloading", False)
            is_finished = getattr(st, "is_finished", False)
            num_peers = getattr(st, "num_peers", 0) or 0
            progress = getattr(st, "progress", 0.0) or 0.0
            dl_rate = getattr(st, "download_rate", 0) or 0

            # update live metadata for dashboard
            active_downloads[magnet].update({
                "status": "Downloading" if is_downloading else ("Finished" if is_finished else "Connecting"),
                "progress": round(progress, 2) if isinstance(progress, (float, int)) else progress,
                "download_speed_bps": dl_rate,
                "peers": num_peers,
                "eta": getattr(st, "eta", None)
            })

            # peer timeout handling
            if num_peers == 0:
                if no_peer_start is None:
                    no_peer_start = time.time()
                elif time.time() - no_peer_start > PEER_TIMEOUT_SECONDS:
                    # auto cancel
                    print(f"[AUTO-CANCEL] No peers for {PEER_TIMEOUT_SECONDS}s for magnet: {magnet}")
                    try:
                        await maybe_await(torrent.stop_download)
                    except Exception:
                        pass
                    active_downloads.pop(magnet, None)
                    await save_state()
                    return
            else:
                no_peer_start = None

            # finished?
            if is_finished:
                # collect files from torrent.files if available otherwise fallback
                files = getattr(torrent, "files", None)
                if files:
                    for f in files:
                        path = (DOWNLOAD_DIR / f).resolve()
                        # ensure file inside download dir
                        if path.exists() and DOWNLOAD_DIR in path.parents or path.parent == DOWNLOAD_DIR:
                            size = path.stat().st_size
                            completed_files.append({
                                "magnet": magnet,
                                "filename": f,
                                "size": size,
                                "completed_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                            })
                else:
                    # fallback attempt to use a name in status
                    sname = getattr(torrent.status, "name", None)
                    if sname:
                        path = (DOWNLOAD_DIR / sname).resolve()
                        if path.exists():
                            size = path.stat().st_size
                            completed_files.append({
                                "magnet": magnet,
                                "filename": sname,
                                "size": size,
                                "completed_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
                            })

                # persist and remove from active
                await save_state()
                print(f"[DONE] magnet: {magnet}")
                active_downloads.pop(magnet, None)
                # stop torrent properly
                try:
                    await maybe_await(torrent.stop_download)
                except Exception:
                    pass
                return

            await asyncio.sleep(1)

    except asyncio.CancelledError:
        print(f"[CANCELLED] {magnet}")
        try:
            await maybe_await(torrent.stop_download)
        except Exception:
            pass
        active_downloads.pop(magnet, None)
        await save_state()
    except Exception as exc:
        print(f"[ERROR] download loop for {magnet}: {exc}")
        active_downloads.pop(magnet, None)
        await save_state()

# ========== FASTAPI STARTUP ==========
@app.on_event("startup")
async def on_startup():
    await load_state()
    # resume worker
    asyncio.create_task(download_worker())
    # autosave loop to ensure frequent persistence (saves every 10s if there are changes)
    asyncio.create_task(autosave_loop())

async def autosave_loop():
    last_saved = 0
    while True:
        # save every 10 seconds
        if time.time() - last_saved > 10:
            await save_state()
            last_saved = time.time()
        await asyncio.sleep(5)

# ========== ROUTES ==========
@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML

@app.post("/add")
async def add_torrent(magnet: str = Form(...)):
    magnet = magnet.strip()
    if not magnet:
        raise HTTPException(status_code=400, detail="Empty magnet")
    # dedupe: check active, queue, completed
    if magnet in active_downloads or magnet in queue or any(c["magnet"] == magnet for c in completed_files):
        return JSONResponse({"status": "exists"})
    queue.append(magnet)
    await save_state()
    return JSONResponse({"status": "queued", "queue_position": len(queue)})

@app.get("/status")
async def status():
    # Build response from in-memory state (active_downloads, queue, completed_files)
    active = []
    for magnet, info in active_downloads.items():
        # info may either be dict metadata or TorrentDownloader instance container
        if isinstance(info, dict) and info.get("torrent"):
            st = getattr(info["torrent"], "status", None)
        elif hasattr(info, "status"):
            st = getattr(info, "status", None)
        else:
            st = None

        entry = {"magnet": magnet}
        if st:
            entry.update({
                "name": getattr(st, "name", None),
                "progress": round(getattr(st, "progress", 0.0) or 0.0, 2),
                "peers": getattr(st, "num_peers", None),
                "download_speed_bps": getattr(st, "download_rate", None),
                "eta": getattr(st, "eta", None),
                "is_downloading": getattr(st, "is_downloading", False),
                "is_finished": getattr(st, "is_finished", False),
            })
        else:
            # if no status object, try to report stored metadata
            entry.update({
                "status": info.get("status") if isinstance(info, dict) else "starting"
            })
        active.append(entry)

    return {
        "active": active,
        "queue": list(queue),
        "completed": completed_files,
        "download_dir": str(DOWNLOAD_DIR)
    }

@app.post("/cancel")
async def cancel(request: Request):
    body = await request.json()
    magnet = body.get("magnet")
    if not magnet:
        raise HTTPException(status_code=400, detail="Provide {'magnet': '...'} in JSON body")
    # cancel active
    if magnet in active_downloads:
        info = active_downloads[magnet]
        task = None
        torrent = None
        if isinstance(info, dict):
            task = info.get("task")
            torrent = info.get("torrent")
        elif hasattr(info, "status"):
            torrent = info
        if task and not task.done():
            task.cancel()
        elif torrent:
            try:
                await maybe_await(torrent.stop_download)
            except Exception:
                pass
        active_downloads.pop(magnet, None)
        await save_state()
        return {"status": "cancelled"}
    # remove from queue
    if magnet in queue:
        try:
            queue.remove(magnet)
        except ValueError:
            pass
        await save_state()
        return {"status": "removed_from_queue"}
    return {"status": "not_found"}

@app.get("/file/{filename}")
def download_file(filename: str):
    # sanitize / prevent path traversal
    fname = Path(filename).name
    path = (DOWNLOAD_DIR / fname).resolve()
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    # ensure file inside download dir
    if DOWNLOAD_DIR not in path.parents and path.parent != DOWNLOAD_DIR:
        raise HTTPException(status_code=403, detail="Forbidden")
    return FileResponse(str(path), filename=fname)

# ========== DASHBOARD HTML (Bootstrap + Cancel buttons) ==========
DASHBOARD_HTML = f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width,initial-scale=1"/>
    <title>Torrent Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"/>
    <style> body{{padding:20px;background:#f8f9fa}} .progress{{height:22px}} .small-muted{{color:#6c757d}} </style>
  </head>
  <body>
    <div class="container">
      <h1 class="mb-4">📥 Torrent Dashboard</h1>

      <div class="card mb-3">
        <div class="card-body">
          <form id="addForm" class="row g-2">
            <div class="col-md-9">
              <input name="magnet" class="form-control" placeholder="Paste magnet link here" required />
            </div>
            <div class="col-md-3">
              <button class="btn btn-primary w-100" type="submit">Add to Queue</button>
            </div>
          </form>
        </div>
      </div>

      <div class="row">
        <div class="col-lg-6">
          <div class="card mb-3">
            <div class="card-header bg-primary text-white">Active Downloads</div>
            <ul class="list-group list-group-flush" id="activeList"><li class="list-group-item text-muted">Loading...</li></ul>
          </div>

          <div class="card mb-3">
            <div class="card-header bg-warning">Queue</div>
            <ul class="list-group list-group-flush" id="queueList"><li class="list-group-item text-muted">Loading...</li></ul>
          </div>
        </div>

        <div class="col-lg-6">
          <div class="card mb-3">
            <div class="card-header bg-success text-white">Completed</div>
            <ul class="list-group list-group-flush" id="completedList"><li class="list-group-item text-muted">Loading...</li></ul>
          </div>

          <div class="card mb-3">
            <div class="card-body">
              <p class="mb-1"><strong>Download directory</strong></p>
              <p class="small-muted" id="downloadDir">{DOWNLOAD_DIR}</p>
              <p class="mb-0 small-muted">Make sure Render has a persistent disk mounted to this path.</p>
            </div>
          </div>
        </div>
      </div>

    </div>

    <script>
      async function refreshUI() {{
        try {{
          const res = await fetch('/status');
          const data = await res.json();

          // Active
          const activeList = document.getElementById('activeList');
          activeList.innerHTML = '';
          if (data.active.length === 0) {{
            activeList.innerHTML = '<li class="list-group-item text-muted">No active downloads</li>';
          }} else {{
            for (const t of data.active) {{
              const speedKb = t.download_speed_bps ? Math.round(t.download_speed_bps / 1024) : 0;
              const progress = t.progress !== undefined ? t.progress : 0;
              activeList.innerHTML += `
                <li class="list-group-item">
                  <div class="d-flex justify-content-between">
                    <div><strong>${{t.name || t.magnet}}</strong><br><small class="text-muted">${{t.magnet}}</small></div>
                    <div>
                      <button class="btn btn-sm btn-outline-danger" onclick="cancelMagnet('${{encodeURIComponent(t.magnet)}}')">Cancel</button>
                    </div>
                  </div>
                  <div class="mt-2">
                    <div class="progress">
                      <div class="progress-bar progress-bar-striped progress-bar-animated" role="progressbar" style="width:${{progress}}%">${{progress}}%</div>
                    </div>
                    <div class="mt-1 small text-muted">${{speedKb}} kB/s | Peers: ${{t.peers ?? 'N/A'}} | ETA: ${{t.eta ?? 'N/A'}}</div>
                  </div>
                </li>`;
            }}
          }}

          // Queue
          const queueList = document.getElementById('queueList');
          queueList.innerHTML = '';
          if (data.queue.length === 0) {{
            queueList.innerHTML = '<li class="list-group-item text-muted">Queue is empty</li>';
          }} else {{
            data.queue.forEach((m, i) => {{
              queueList.innerHTML += `<li class="list-group-item d-flex justify-content-between align-items-start">
                <div class="me-2"><small class="text-muted">${{i+1}}.</small> <span class="text-break">${{m}}</span></div>
                <button class="btn btn-sm btn-outline-secondary" onclick="cancelMagnet('${{encodeURIComponent(m)}}')">Remove</button>
              </li>`;
            }});
          }}

          // Completed
          const completedList = document.getElementById('completedList');
          completedList.innerHTML = '';
          if (data.completed.length === 0) {{
            completedList.innerHTML = '<li class="list-group-item text-muted">No completed files</li>';
          }} else {{
            for (const c of data.completed) {{
              const sizeMB = (c.size / (1024*1024)).toFixed(2);
              completedList.innerHTML += `<li class="list-group-item">
                <div><a href="/file/${{encodeURIComponent(c.filename)}}">${{c.filename}}</a></div>
                <div class="small text-muted">${{sizeMB}} MB • ${{c.completed_at}}</div>
              </li>`;
            }}
          }}

        }} catch (err) {{
          console.error("Refresh error:", err);
        }}
      }}

      async function cancelMagnet(magnetEncoded) {{
        const magnet = decodeURIComponent(magnetEncoded);
        try {{
          await fetch('/cancel', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ magnet }})
          }});
        }} catch (e) {{
          console.error("Cancel failed", e);
        }}
        setTimeout(refreshUI, 500);
      }}

      document.getElementById('addForm').onsubmit = async (e) => {{
        e.preventDefault();
        const fd = new FormData(e.target);
        await fetch('/add', {{ method: 'POST', body: fd }});
        e.target.reset();
        setTimeout(refreshUI, 300);
      }};

      setInterval(refreshUI, 2000);
      refreshUI();
    </script>
  </body>
</html>
"""

# If this file is run directly, start a development server (helpful for local testing).
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
