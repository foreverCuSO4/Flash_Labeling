"""Brush annotation API (docs/auto_labeling.md item 2).

POST /api/images/{image_id}/brush {x, y, r} → detection suggestion inside the
brush circle at the model's floor confidence (0.05). The winning detector
class is returned and applied on the client when it maps
to a project class; otherwise the annotator's currently selected class is used.
"""
from typing import Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from .. import detector as detector_mod
from ..autolabel import brush_hit, row_box_norm, row_class_index, row_corners_norm, row_score
from ..config import UPLOAD_DIR
from ..db import get_session
from ..models import Image, User
from ..security import current_user, require_member
from .images import claim_expired

router = APIRouter(tags=["autolabel"])


class BrushIn(BaseModel):
    x: float = Field(ge=0, le=1)   # normalized click position
    y: float = Field(ge=0, le=1)
    r: float = Field(gt=0, le=2000)  # brush radius in image pixels


@router.post("/api/images/{image_id}/brush")
def brush(image_id: int, body: BrushIn,
          session: Session = Depends(get_session),
          user: User = Depends(current_user)):
    img = session.get(Image, image_id)
    if img is None:
        raise HTTPException(404, "image not found")
    project, _ = require_member(img.project_id, user, session)
    if img.claimed_by != user.id or claim_expired(img):
        raise HTTPException(403, "image not claimed by you")

    rows = detector_mod.read_cache(img.project_id, img.stored_name, project.model_id)
    cached = rows is not None
    if rows is None:
        rows = _detect_and_cache(img, project.model_id)

    hit = brush_hit(rows, body.x, body.y, body.r, img.width, img.height)
    if hit is None:
        return {"suggestion": None, "cached": cached,
                "rows": int(len(rows))}
    x, y, w, h = row_box_norm(hit)
    return {
        "suggestion": {"x": x, "y": y, "w": w, "h": h,
                       "corners": row_corners_norm(hit),
                       "class_index": row_class_index(hit),
                       "score": row_score(hit)},
        "cached": cached,
        "rows": int(len(rows)),
    }


def _detect_and_cache(img: Image, model_id: int | None = None) -> np.ndarray:
    """On-demand single-frame detection for images without a cache
    (e.g. plain uploads, or jobs run before auto-scan existed)."""
    import cv2

    path = UPLOAD_DIR / str(img.project_id) / img.stored_name
    if not path.exists():
        raise HTTPException(404, "image file missing")
    frame = cv2.imread(str(path))
    if frame is None:
        raise HTTPException(400, "cannot decode image")
    if frame.shape[:2] != (384, 640):
        frame = cv2.resize(frame, (640, 384))
    try:
        rows = detector_mod.detect_frames(frame[None], model_id=model_id)[0]
    except detector_mod.DetectorUnavailable as e:
        raise HTTPException(503, str(e)) from e
    detector_mod.write_cache(img.project_id, img.stored_name, rows, model_id=model_id)
    return rows
