import json
import shutil
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from ..config import UPLOAD_DIR, VIDEO_DIR
from ..db import engine, get_session
from ..models import Image, User, VideoJob
from ..security import current_user, require_member
from ..video import Cancelled, ExtractParams, ParamsError, extract_frames

router = APIRouter(prefix="/api/projects/{project_id}/videos", tags=["videos"])

ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
MAX_VIDEO_BYTES = 4 * 1024 ** 3  # 4 GiB per video
_CHUNK = 1 << 20  # 1 MiB streaming read


def job_out(job: VideoJob) -> dict:
    return {
        "id": job.id,
        "filename": job.filename,
        "status": job.status,
        "cancel_requested": job.cancel_requested,
        "progress": job.progress,
        "fps": job.fps,
        "total_frames": job.total_frames,
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
        job.status = "running"
        session.add(job)
        session.commit()

        upload_dir = UPLOAD_DIR / str(job.project_id)
        tmp_dir = upload_dir / f".job{job.id}"
        params = ExtractParams.from_dict(json.loads(job.params))

        def on_progress(decoded: int, total: int, extracted: int) -> None:
            job.progress = min(1.0, decoded / total) if total else 0.0
            job.total_frames = total
            job.extracted_frames = extracted
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
async def upload_videos(
    project_id: int,
    files: list[UploadFile],
    params: str = Form("{}"),
    deps=Depends(require_member),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    try:
        extract_params = ExtractParams.from_dict(json.loads(params))
    except json.JSONDecodeError:
        raise HTTPException(400, "params must be a JSON object")
    except ParamsError as e:
        raise HTTPException(400, f"invalid params: {e}")

    dest_dir = VIDEO_DIR / str(project_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_VIDEO_EXT:
            raise HTTPException(400, f"unsupported video type: {file.filename}")
        stored = f"{uuid.uuid4().hex}{ext}"
        dest = dest_dir / stored
        size = 0
        try:
            with open(dest, "wb") as fh:
                while chunk := await file.read(_CHUNK):
                    size += len(chunk)
                    if size > MAX_VIDEO_BYTES:
                        raise HTTPException(400, f"video too large: {file.filename}")
                    fh.write(chunk)
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
        threading.Thread(target=run_job, args=(job.id,), daemon=True).start()
        jobs.append(job_out(job))
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
