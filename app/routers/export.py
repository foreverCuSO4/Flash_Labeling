import json
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from ..config import UPLOAD_DIR
from ..db import get_session
from ..models import Annotation, Image, ProjectClass
from ..security import require_member

router = APIRouter(prefix="/api/projects/{project_id}/export", tags=["export"])


def _data_yaml(project, classes: list[ProjectClass]) -> str:
    lines = [
        "# YOLO dataset config exported from Flash Labeling",
        "path: .",
        "train: images",
        "val: images",
        "",
        "names:",
    ]
    for c in classes:
        lines.append(f"  {c.ord}: {json.dumps(c.name)}")
    if project.mode == "pose":
        n_kpts = len(json.loads(project.keypoints or "[]"))
        lines += ["", f"kpt_shape: [{n_kpts}, 3]"]
    return "\n".join(lines) + "\n"


def _label_line(ann: Annotation, cls: ProjectClass) -> str:
    parts = [str(cls.ord), f"{ann.x:.6f}", f"{ann.y:.6f}", f"{ann.w:.6f}", f"{ann.h:.6f}"]
    if ann.keypoints:
        for kp in json.loads(ann.keypoints):
            parts += [f"{kp['x']:.6f}", f"{kp['y']:.6f}", str(kp["v"])]
    return " ".join(parts)


@router.get("")
def export_yolo(project_id: int, deps=Depends(require_member), session: Session = Depends(get_session)):
    """YOLO dataset zip: images/ + labels/ + classes.txt + data.yaml.

    Detection: each label line is `class cx cy w h`.
    Pose: each label line is `class cx cy w h kp1x kp1y kp1v ...`.
    """
    project, _ = deps
    classes = session.exec(
        select(ProjectClass).where(ProjectClass.project_id == project_id).order_by(ProjectClass.ord)
    ).all()
    images = session.exec(select(Image).where(Image.project_id == project_id).order_by(Image.id)).all()

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("classes.txt", "\n".join(c.name for c in classes) + "\n")
        zf.writestr("data.yaml", _data_yaml(project, classes))
        for img in images:
            src = UPLOAD_DIR / str(project_id) / img.stored_name
            if not src.exists():
                continue
            ext = Path(img.stored_name).suffix
            zf.write(src, f"images/{img.id}{ext}")
            anns = session.exec(select(Annotation).where(Annotation.image_id == img.id)).all()
            lines = []
            for a in anns:
                cls = session.get(ProjectClass, a.class_id)
                if cls is None:
                    continue
                lines.append(_label_line(a, cls))
            zf.writestr(f"labels/{img.id}.txt", "\n".join(lines) + ("\n" if lines else ""))
    buf.seek(0)
    filename = f"{project.name.replace(' ', '_')}_yolo.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
