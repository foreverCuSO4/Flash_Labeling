import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import UPLOAD_DIR, UPLOAD_TMP_DIR, VIDEO_DIR
from ..db import engine, get_session
from ..models import Image, Project, User, VideoJob
from ..security import current_user, get_membership, require_member
from ..video import Cancelled, ExtractParams, ParamsError, extract_frames

router = APIRouter(prefix="/api/projects/{project_id}/videos", tags=["videos"])

ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
MAX_VIDEO_BYTES = 4 * 1024 ** 3  # 4 GiB per video
_CHUNK = 1 << 20  # 1 MiB streaming read

# Bound concurrent video-file saves — each is a multi-hundred-MB disk write,
# and several at once can stall the host's I/O.
_SAVE_SEM = threading.Semaphore(2)

# Per-video extraction parallelism: the video is split into frame ranges
# decoded concurrently (each thread runs its own decoder). Default 4 —
# enough to speed up a single big video several-fold without swamping a
# small host; override with VIDEO_EXTRACT_WORKERS.
_EXTRACT_WORKERS = max(1, int(os.environ.get("VIDEO_EXTRACT_WORKERS", "4")))

# Server-side save progress, so the browser can show a bar after the request
# body has been sent (XHR progress only covers the wire). upload_id is a
# client-generated uuid; entries self-prune after the TTL.
_UPLOAD_PROGRESS: dict[str, dict] = {}
_UPLOAD_LOCK = threading.Lock()
_UPLOAD_TTL = 600


def _upload_update(upload_id: str, **fields) -> None:
    if not upload_id:
        return
    now = time.time()
    with _UPLOAD_LOCK:
        stale = [k for k, v in _UPLOAD_PROGRESS.items() if now - v.get("ts", now) > _UPLOAD_TTL]
        for k in stale:
            _UPLOAD_PROGRESS.pop(k, None)
        entry = _UPLOAD_PROGRESS.setdefault(upload_id, {})
        entry.update(fields)
        entry["ts"] = now


uploads_router = APIRouter(tags=["uploads"])


@uploads_router.get("/api/uploads/{upload_id}/progress")
def upload_progress(upload_id: str, user: User = Depends(current_user)):
    with _UPLOAD_LOCK:
        entry = dict(_UPLOAD_PROGRESS.get(upload_id) or {})
    if not entry or entry.get("user_id") != user.id:
        raise HTTPException(404, "unknown upload")
    entry.pop("user_id", None)
    return entry


# ---------------------------------------------------------------------------
# Chunked resumable video upload.
#
# Flow: POST /api/uploads {filename, size} -> upload_id; then PUT
# /api/uploads/<id>/chunk?offset=N with raw bytes. Chunks may arrive in any
# order and concurrently (clients upload several in parallel): the server
# tracks received byte ranges in the sidecar and reports the longest received
# prefix as `received`. Resends of covered bytes are idempotent no-ops.
# POST /api/uploads/<id>/complete {project_id, params} turns the fully received
# file into an extraction job. Stale partials (24h) are swept on init.
# ---------------------------------------------------------------------------

_STALE_SECONDS = 24 * 3600
_ID_RE = re.compile(r"[0-9A-Za-z-]{1,64}")
_CHUNK_LOCK = threading.Lock()


def _sidecar_path(upload_id: str) -> Path:
    return UPLOAD_TMP_DIR / f"{upload_id}.json"


def _part_path(upload_id: str) -> Path:
    return UPLOAD_TMP_DIR / f"{upload_id}.part"


def _write_meta(upload_id: str, meta: dict) -> None:
    _sidecar_path(upload_id).write_text(json.dumps(meta))


def _ranges_of(meta: dict) -> list[list[int]]:
    """Received byte ranges; older sidecars only had a contiguous `received` prefix."""
    if "ranges" in meta:
        return meta["ranges"]
    return [[0, meta["received"]]] if meta.get("received") else []


def _merge_range(ranges: list[list[int]], start: int, end: int) -> list[list[int]]:
    """Insert [start, end) and merge overlapping/adjacent ranges. Sorted output."""
    ranges.append([start, end])
    ranges.sort()
    merged = [ranges[0]]
    for s, e in ranges[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def _contiguous_prefix(ranges: list[list[int]]) -> int:
    """Longest received prefix starting at 0."""
    if ranges and ranges[0][0] == 0:
        return ranges[0][1]
    return 0


def _load_meta_owned(upload_id: str, user_id: int) -> dict:
    if not _ID_RE.fullmatch(upload_id):  # also blocks path traversal
        raise HTTPException(404, "upload not found")
    sc = _sidecar_path(upload_id)
    if not sc.exists():
        raise HTTPException(404, "upload not found")
    meta = json.loads(sc.read_text())
    if meta.get("user_id") != user_id:
        raise HTTPException(404, "upload not found")  # don't leak existence
    return meta


def _sweep_stale_uploads() -> None:
    now = time.time()
    for sc in UPLOAD_TMP_DIR.glob("*.json"):
        try:
            created = json.loads(sc.read_text()).get("created_at", now)
        except Exception:
            created = 0
        if now - created > _STALE_SECONDS:
            sc.unlink(missing_ok=True)
            (UPLOAD_TMP_DIR / f"{sc.stem}.part").unlink(missing_ok=True)


class UploadInitIn(BaseModel):
    filename: str
    size: int


class UploadCompleteIn(BaseModel):
    project_id: int
    params: dict = {}


@uploads_router.post("/api/uploads")
def init_upload(body: UploadInitIn, user: User = Depends(current_user)):
    name = Path(body.filename).name  # strip any path components
    if Path(name).suffix.lower() not in ALLOWED_VIDEO_EXT:
        raise HTTPException(400, f"unsupported video type: {name}")
    if not (0 < body.size <= MAX_VIDEO_BYTES):
        raise HTTPException(400, f"invalid file size: {body.size}")
    _sweep_stale_uploads()
    upload_id = uuid.uuid4().hex
    _write_meta(upload_id, {
        "filename": name, "size": body.size, "received": 0,
        "user_id": user.id, "created_at": time.time(),
    })
    _part_path(upload_id).touch()
    return {"upload_id": upload_id}


@uploads_router.get("/api/uploads/{upload_id}")
def get_upload(upload_id: str, user: User = Depends(current_user)):
    """Resume info — received prefix plus all received ranges (for parallel clients)."""
    meta = _load_meta_owned(upload_id, user.id)
    ranges = _ranges_of(meta)
    return {
        "upload_id": upload_id,
        "filename": meta["filename"],
        "size": meta["size"],
        "received": _contiguous_prefix(ranges),
        "ranges": ranges,
    }


@uploads_router.put("/api/uploads/{upload_id}/chunk")
async def upload_chunk(upload_id: str, request: Request, offset: int = 0,
                       user: User = Depends(current_user)):
    meta = _load_meta_owned(upload_id, user.id)
    if offset < 0:
        raise HTTPException(400, "offset must be >= 0")
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty chunk")
    end = offset + len(data)
    if end > meta["size"]:
        raise HTTPException(400, "chunk exceeds the declared file size")
    with _CHUNK_LOCK:
        # Re-read the sidecar under the lock: concurrent chunks each merge into
        # the latest on-disk state (reading it earlier would clobber ranges).
        meta = _load_meta_owned(upload_id, user.id)
        ranges = _ranges_of(meta)
        if any(s <= offset and end <= e for s, e in ranges):
            received = _contiguous_prefix(ranges)
            return {"received": received}  # fully covered resend — idempotent no-op
        # Positioned write: chunks may land out of order and concurrently.
        with open(_part_path(upload_id), "r+b") as fh:
            fh.seek(offset)
            fh.write(data)
        ranges = _merge_range(ranges, offset, end)
        received = _contiguous_prefix(ranges)
        meta["ranges"] = ranges
        meta["received"] = received
        _write_meta(upload_id, meta)
    return {"received": received}


@uploads_router.post("/api/uploads/{upload_id}/complete")
def complete_upload(upload_id: str, body: UploadCompleteIn,
                    user: User = Depends(current_user), session: Session = Depends(get_session)):
    meta = _load_meta_owned(upload_id, user.id)
    if meta["received"] < meta["size"]:
        raise HTTPException(409, detail={"received": meta["received"]})
    project = session.get(Project, body.project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if get_membership(session, body.project_id, user.id) is None:
        raise HTTPException(403, "not a project member")
    try:
        extract_params = ExtractParams.from_dict(body.params)
    except ParamsError as e:
        raise HTTPException(400, f"invalid params: {e}")

    # Same DATA_DIR volume -> the assembled file moves into place atomically.
    ext = Path(meta["filename"]).suffix.lower()
    stored = f"{uuid.uuid4().hex}{ext}"
    dest_dir = VIDEO_DIR / str(body.project_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    Path(_part_path(upload_id)).replace(dest_dir / stored)
    _sidecar_path(upload_id).unlink(missing_ok=True)

    job = VideoJob(
        project_id=body.project_id,
        filename=meta["filename"],
        stored_name=stored,
        params=json.dumps(extract_params.to_dict()),
        created_by=user.id,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    _enqueue(job.id)
    return job_out(job)


# Jobs are extracted one at a time by a single worker: video decode saturates
# every CPU core (OpenCV is internally multithreaded) and hammers the disk with
# JPEGs, so running several at once starves the whole host.
_JOB_QUEUE: queue.Queue[int] = queue.Queue()
_WORKER_LOCK = threading.Lock()
_WORKER_STARTED = False


def _enqueue(job_id: int) -> None:
    global _WORKER_STARTED
    _JOB_QUEUE.put(job_id)
    with _WORKER_LOCK:
        if not _WORKER_STARTED:
            _WORKER_STARTED = True
            threading.Thread(target=_worker_loop, daemon=True).start()


def _worker_loop() -> None:
    while True:
        job_id = _JOB_QUEUE.get()
        try:
            run_job(job_id)
        except Exception:  # a crashed job must not kill the worker
            import logging
            logging.getLogger("videos").exception("job %s failed hard", job_id)


def job_out(job: VideoJob) -> dict:
    return {
        "id": job.id,
        "filename": job.filename,
        "status": job.status,
        "cancel_requested": job.cancel_requested,
        "progress": job.progress,
        "fps": job.fps,
        "total_frames": job.total_frames,
        "decoded_frames": job.decoded_frames,
        "extracted_frames": job.extracted_frames,
        "params": json.loads(job.params),
        "error": job.error,
        "created_at": job.created_at.isoformat(),
    }


def _cleanup_paths(*paths: Path) -> None:
    for p in paths:
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def run_job(job_id: int) -> None:
    """Background worker: decode, sample frames, register them as images.

    Runs in a daemon thread with its own DB session (engine is created with
    check_same_thread=False). Frames land in a per-job temp dir and are moved
    into the uploads dir only on success, so partial output never leaks.
    """
    with Session(engine) as session:
        job = session.get(VideoJob, job_id)
        if job is None or job.status != "pending":
            return
        if job.cancel_requested:  # cancelled while queued
            job.status = "cancelled"
            session.add(job)
            session.commit()
            return
        job.status = "running"
        session.add(job)
        session.commit()

        upload_dir = UPLOAD_DIR / str(job.project_id)
        tmp_dir = upload_dir / f".job{job.id}"
        params = ExtractParams.from_dict(json.loads(job.params))

        def on_progress(decoded: int, total: int, extracted: int) -> None:
            job.decoded_frames = decoded
            job.total_frames = total
            job.extracted_frames = extracted
            # total == 0 means the container doesn't know its frame count —
            # progress stays 0 (indeterminate) rather than dividing by garbage.
            job.progress = min(1.0, decoded / total) if total else 0.0
            session.add(job)
            session.commit()

        def should_cancel() -> bool:
            session.refresh(job)
            return job.cancel_requested

        try:
            result = extract_frames(
                VIDEO_DIR / str(job.project_id) / job.stored_name,
                tmp_dir,
                params,
                workers=_EXTRACT_WORKERS,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
        except Cancelled:
            job.status = "cancelled"
            session.add(job)
            session.commit()
            _cleanup_paths(tmp_dir)
            return
        except Exception as e:
            job.status = "failed"
            job.error = str(e)[:500]
            session.add(job)
            session.commit()
            _cleanup_paths(tmp_dir)
            return

        upload_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(job.filename).stem
        for f in result["frames"]:
            shutil.move(str(tmp_dir / f["stored_name"]), str(upload_dir / f["stored_name"]))
            session.add(Image(
                project_id=job.project_id,
                filename=f"{stem}_f{f['frame_idx']:06d}.jpg",
                stored_name=f["stored_name"],
                width=f["width"],
                height=f["height"],
                uploaded_by=job.created_by,
            ))
        job.status = "done"
        job.progress = 1.0
        job.fps = result["fps"]
        job.total_frames = result["total_frames"]
        job.decoded_frames = result["total_frames"]
        job.extracted_frames = len(result["frames"])
        session.add(job)
        session.commit()
        _cleanup_paths(tmp_dir)


@router.get("")
def list_jobs(project_id: int, deps=Depends(require_member), session: Session = Depends(get_session)):
    jobs = session.exec(
        select(VideoJob).where(VideoJob.project_id == project_id).order_by(VideoJob.id.desc())
    ).all()
    return [job_out(j) for j in jobs]


@router.post("/upload")
def upload_videos(
    project_id: int,
    files: list[UploadFile],
    params: str = Form("{}"),
    upload_id: str = Form(""),
    deps=Depends(require_member),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    # Sync endpoint on purpose: it runs in FastAPI's threadpool, so the
    # synchronous disk writes below (hundreds of MB per video) never block the
    # event loop — polling and other requests stay responsive during uploads.
    try:
        extract_params = ExtractParams.from_dict(json.loads(params))
    except json.JSONDecodeError:
        raise HTTPException(400, "params must be a JSON object")
    except ParamsError as e:
        raise HTTPException(400, f"invalid params: {e}")

    dest_dir = VIDEO_DIR / str(project_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    try:
        for file_idx, file in enumerate(files):
            ext = Path(file.filename or "").suffix.lower()
            if ext not in ALLOWED_VIDEO_EXT:
                raise HTTPException(400, f"unsupported video type: {file.filename}")
            stored = f"{uuid.uuid4().hex}{ext}"
            dest = dest_dir / stored
            size = 0
            with _SAVE_SEM:
                _upload_update(upload_id, user_id=user.id, filename=file.filename,
                               file_index=file_idx, file_count=len(files),
                               saved=0, total=file.size, done=False)
                try:
                    with open(dest, "wb") as fh:
                        while chunk := file.file.read(_CHUNK):
                            size += len(chunk)
                            if size > MAX_VIDEO_BYTES:
                                raise HTTPException(400, f"video too large: {file.filename}")
                            fh.write(chunk)
                            _upload_update(upload_id, saved=size)
                except Exception:
                    dest.unlink(missing_ok=True)
                    raise
            if size == 0:
                dest.unlink(missing_ok=True)
                raise HTTPException(400, f"empty file: {file.filename}")
            job = VideoJob(
                project_id=project_id,
                filename=file.filename or stored,
                stored_name=stored,
                params=json.dumps(extract_params.to_dict()),
                created_by=user.id,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            _enqueue(job.id)
            jobs.append(job_out(job))
        _upload_update(upload_id, done=True)
    except Exception:
        _upload_update(upload_id, error=True)
        raise
    return jobs


@router.post("/{job_id}/cancel")
def cancel_job(
    project_id: int,
    job_id: int,
    deps=Depends(require_member),
    session: Session = Depends(get_session),
):
    job = session.get(VideoJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(404, "job not found")
    if job.status in ("pending", "running"):
        job.cancel_requested = True
        session.add(job)
        session.commit()
        session.refresh(job)
    return job_out(job)
