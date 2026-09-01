import uuid
from datetime import datetime, timezone
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image as PILImage
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..config import UPLOAD_DIR
from ..db import get_session
from ..models import Annotation, Image, Project, ProjectClass, User
from ..security import current_user, require_member

router = APIRouter(prefix="/api/projects/{project_id}/images", tags=["images"])

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
CLAIM_TIMEOUT_HOURS = 24


class ClaimBatchIn(BaseModel):
    count: int = Field(ge=1, le=500)


def claim_expired(img: Image) -> bool:
    if img.claimed_by is None or img.claimed_at is None:
        return False
    elapsed = (datetime.now(timezone.utc) - img.claimed_at.replace(tzinfo=timezone.utc)).total_seconds()
    return elapsed > CLAIM_TIMEOUT_HOURS * 3600


def image_out(img: Image, session: Session) -> dict:
    claimer = session.get(User, img.claimed_by) if img.claimed_by else None
    ann_count = len(session.exec(select(Annotation).where(Annotation.image_id == img.id)).all())
    return {
        "id": img.id,
        "filename": img.filename,
        "width": img.width,
        "height": img.height,
        "status": img.status,
        "claimed_by": img.claimed_by,
        "claimed_by_name": claimer.name if claimer else None,
        "claim_expired": claim_expired(img),
        "annotation_count": ann_count,
        "created_at": img.created_at.isoformat(),
        "url": f"/api/images/{img.id}/file",
    }


def save_upload(file: UploadFile, project_id: int) -> tuple[str, int, int, str]:
    raw = file.file.read()
    if not raw:
        raise HTTPException(400, f"empty file: {file.filename}")
    try:
        with PILImage.open(BytesIO(raw)) as im:
            im.verify()
        with PILImage.open(BytesIO(raw)) as im:
            width, height = im.size
            fmt = (im.format or "").lower()
    except Exception:
        raise HTTPException(400, f"not a valid image: {file.filename}")
    ext = {"jpeg": ".jpg", "png": ".png", "bmp": ".bmp", "webp": ".webp"}.get(fmt)
    if ext is None:
        raise HTTPException(400, f"unsupported image type: {file.filename}")
    stored = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / str(project_id)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / stored).write_bytes(raw)
    return stored, width, height, ext


@router.get("")
def list_images(project_id: int, deps=Depends(require_member), session: Session = Depends(get_session)):
    images = session.exec(
        select(Image).where(Image.project_id == project_id).order_by(Image.id)
    ).all()
    return [image_out(img, session) for img in images]


@router.post("/upload")
async def upload_images(
    project_id: int,
    files: list[UploadFile],
    deps=Depends(require_member),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    out = []
    for file in files:
        stored, width, height, _ = save_upload(file, project_id)
        img = Image(
            project_id=project_id,
            filename=file.filename or stored,
            stored_name=stored,
            width=width,
            height=height,
            uploaded_by=user.id,
        )
        session.add(img)
        session.commit()
        session.refresh(img)
        out.append(image_out(img, session))
    return out


@router.post("/claim")
def claim_batch(
    project_id: int,
    body: ClaimBatchIn,
    deps=Depends(require_member),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Claim up to `count` oldest unlabeled images not currently under an active claim."""
    candidates = session.exec(
        select(Image)
        .where(Image.project_id == project_id, Image.status == "unlabeled")
        .order_by(Image.id)
    ).all()
    now = datetime.now(timezone.utc)
    claimed = []
    for img in candidates:
        if len(claimed) >= body.count:
            break
        if img.claimed_by is not None and not claim_expired(img):
            continue  # active claim (anyone's, including ours) — skip
        img.claimed_by = user.id
        img.claimed_at = now
        session.add(img)
        claimed.append(img)
    session.commit()
    return {"count": len(claimed), "claimed": [image_out(img, session) for img in claimed]}


@router.post("/{image_id}/claim")
def claim_image(
    project_id: int, image_id: int,
    deps=Depends(require_member),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    img = session.get(Image, image_id)
    if img is None or img.project_id != project_id:
        raise HTTPException(404, "image not found")
    if img.claimed_by is not None and img.claimed_by != user.id and not claim_expired(img):
        raise HTTPException(409, "image already claimed by another user")
    img.claimed_by = user.id
    img.claimed_at = datetime.now(timezone.utc)
    session.add(img)
    session.commit()
    return image_out(img, session)


@router.post("/{image_id}/release")
def release_image(
    project_id: int, image_id: int,
    deps=Depends(require_member),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    img = session.get(Image, image_id)
    if img is None or img.project_id != project_id:
        raise HTTPException(404, "image not found")
    if img.claimed_by != user.id:
        raise HTTPException(403, "not claimed by you")
    img.claimed_by = None
    img.claimed_at = None
    session.add(img)
    session.commit()
    return image_out(img, session)


@router.delete("/{image_id}")
def delete_image(
    project_id: int, image_id: int,
    deps=Depends(require_member),
    session: Session = Depends(get_session),
):
    img = session.get(Image, image_id)
    if img is None or img.project_id != project_id:
        raise HTTPException(404, "image not found")
    for ann in session.exec(select(Annotation).where(Annotation.image_id == image_id)).all():
        session.delete(ann)
    file_path = UPLOAD_DIR / str(project_id) / img.stored_name
    if file_path.exists():
        file_path.unlink()
    session.delete(img)
    session.commit()
    return {"ok": True}
