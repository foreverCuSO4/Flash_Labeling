import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session
from ..models import Annotation, Image, Project, ProjectClass, User
from ..security import current_user, require_member
from .images import claim_expired

router = APIRouter(tags=["annotations"])


class KeypointIn(BaseModel):
    x: float
    y: float
    v: int = 2  # 0=not labeled, 1=occluded, 2=visible


class BoxIn(BaseModel):
    class_id: int
    x: float
    y: float
    w: float
    h: float
    keypoints: Optional[list[KeypointIn]] = None


def _validate_box(box: BoxIn) -> None:
    if not (0 <= box.x <= 1 and 0 <= box.y <= 1 and 0 < box.w <= 1 and 0 < box.h <= 1):
        raise HTTPException(400, "box coordinates must be normalized to [0,1] with positive size")
    half_w, half_h = box.w / 2, box.h / 2
    if box.x - half_w < -1e-6 or box.x + half_w > 1 + 1e-6 or box.y - half_h < -1e-6 or box.y + half_h > 1 + 1e-6:
        raise HTTPException(400, "box extends outside image bounds")


def _validate_keypoints(box: BoxIn, project: Project) -> None:
    expected = len(json.loads(project.keypoints or "[]"))
    if box.keypoints is None:
        raise HTTPException(400, "pose project requires keypoints on every instance")
    if len(box.keypoints) != expected:
        raise HTTPException(400, f"expected {expected} keypoints, got {len(box.keypoints)}")
    for kp in box.keypoints:
        if kp.v not in (0, 1, 2):
            raise HTTPException(400, "keypoint v must be 0 (not labeled), 1 (occluded) or 2 (visible)")
        if kp.v > 0 and not (0 <= kp.x <= 1 and 0 <= kp.y <= 1):
            raise HTTPException(400, "visible keypoint coordinates must be normalized to [0,1]")


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
        "keypoints": json.loads(a.keypoints) if a.keypoints else None,
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


def _require_active_claim(img: Image, user: User) -> None:
    if img.claimed_by != user.id or claim_expired(img):
        raise HTTPException(403, "image not claimed by you")


@router.put("/api/images/{image_id}/annotations")
def save_annotations(
    image_id: int,
    boxes: list[BoxIn],
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    """Atomic replace: the canvas sends the full box set; we rewrite all rows."""
    img = _get_image_checked(image_id, session)
    project, _ = require_member(img.project_id, user, session)
    _require_active_claim(img, user)
    valid_classes = {
        c.id for c in session.exec(select(ProjectClass).where(ProjectClass.project_id == img.project_id)).all()
    }
    for box in boxes:
        if box.class_id not in valid_classes:
            raise HTTPException(400, f"class_id {box.class_id} not in project")
        _validate_box(box)
        if project.mode == "pose":
            _validate_keypoints(box, project)
        elif box.keypoints is not None:
            raise HTTPException(400, "keypoints are only valid in pose projects")
    for old in session.exec(select(Annotation).where(Annotation.image_id == image_id)).all():
        session.delete(old)
    now = datetime.now(timezone.utc)
    for box in boxes:
        session.add(Annotation(
            image_id=image_id, class_id=box.class_id,
            x=box.x, y=box.y, w=box.w, h=box.h,
            keypoints=json.dumps([kp.model_dump() for kp in box.keypoints]) if box.keypoints else None,
            created_by=user.id, updated_at=now,
        ))
    img.status = "labeled" if boxes else "unlabeled"
    img.labeled_by = user.id if boxes else None
    # The claim is kept after labeling so the annotator can keep fixing and
    # re-saving; it ends via manual release or the 24h lease expiring.
    session.add(img)
    session.commit()
    return {"ok": True, "count": len(boxes)}


@router.delete("/api/images/{image_id}/annotations")
def clear_annotations(image_id: int, session: Session = Depends(get_session), user: User = Depends(current_user)):
    img = _get_image_checked(image_id, session)
    require_member(img.project_id, user, session)
    _require_active_claim(img, user)
    for old in session.exec(select(Annotation).where(Annotation.image_id == image_id)).all():
        session.delete(old)
    img.status = "unlabeled"
    img.labeled_by = None
    session.add(img)
    session.commit()
    return {"ok": True}
