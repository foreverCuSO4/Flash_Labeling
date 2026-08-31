from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session
from ..models import Annotation, Image, ProjectClass, User
from ..security import current_user, require_member

router = APIRouter(tags=["annotations"])


class BoxIn(BaseModel):
    class_id: int
    x: float
    y: float
    w: float
    h: float


def _validate_box(box: BoxIn) -> None:
    if not (0 <= box.x <= 1 and 0 <= box.y <= 1 and 0 < box.w <= 1 and 0 < box.h <= 1):
        raise HTTPException(400, "box coordinates must be normalized to [0,1] with positive size")
    half_w, half_h = box.w / 2, box.h / 2
    if box.x - half_w < -1e-6 or box.x + half_w > 1 + 1e-6 or box.y - half_h < -1e-6 or box.y + half_h > 1 + 1e-6:
        raise HTTPException(400, "box extends outside image bounds")


def _get_image_checked(image_id: int, session: Session) -> Image:
    img = session.get(Image, image_id)
    if img is None:
        raise HTTPException(404, "image not found")
    return img


def ann_out(a: Annotation, cls: ProjectClass) -> dict:
    return {
        "id": a.id, "class_id": a.class_id, "class_name": cls.name if cls else "?",
        "ord": cls.ord if cls else -1,
        "x": a.x, "y": a.y, "w": a.w, "h": a.h,
    }


@router.get("/api/images/{image_id}/annotations")
def list_annotations(image_id: int, session: Session = Depends(get_session), user: User = Depends(current_user)):
    img = _get_image_checked(image_id, session)
    require_member(img.project_id, user, session)
    anns = session.exec(select(Annotation).where(Annotation.image_id == image_id)).all()
    out = []
    for a in anns:
        cls = session.get(ProjectClass, a.class_id)
        out.append(ann_out(a, cls))
    return out


@router.put("/api/images/{image_id}/annotations")
def save_annotations(
    image_id: int,
    boxes: list[BoxIn],
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    """Atomic replace: the canvas sends the full box set; we rewrite all rows."""
    img = _get_image_checked(image_id, session)
    require_member(img.project_id, user, session)
    valid_classes = {
        c.id for c in session.exec(select(ProjectClass).where(ProjectClass.project_id == img.project_id)).all()
    }
    for box in boxes:
        if box.class_id not in valid_classes:
            raise HTTPException(400, f"class_id {box.class_id} not in project")
        _validate_box(box)
    for old in session.exec(select(Annotation).where(Annotation.image_id == image_id)).all():
        session.delete(old)
    now = datetime.now(timezone.utc)
    for box in boxes:
        session.add(Annotation(
            image_id=image_id, class_id=box.class_id,
            x=box.x, y=box.y, w=box.w, h=box.h,
            created_by=user.id, updated_at=now,
        ))
    img.status = "labeled" if boxes else "unlabeled"
    session.add(img)
    session.commit()
    return {"ok": True, "count": len(boxes)}


@router.delete("/api/images/{image_id}/annotations")
def clear_annotations(image_id: int, session: Session = Depends(get_session), user: User = Depends(current_user)):
    img = _get_image_checked(image_id, session)
    require_member(img.project_id, user, session)
    for old in session.exec(select(Annotation).where(Annotation.image_id == image_id)).all():
        session.delete(old)
    img.status = "unlabeled"
    session.add(img)
    session.commit()
    return {"ok": True}
