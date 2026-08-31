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


@router.get("")
def export_yolo(project_id: int, deps=Depends(require_member), session: Session = Depends(get_session)):
    """YOLO dataset zip: images/ + labels/ + classes.txt."""
    project, _ = deps
    classes = session.exec(
        select(ProjectClass).where(ProjectClass.project_id == project_id).order_by(ProjectClass.ord)
    ).all()
    images = session.exec(select(Image).where(Image.project_id == project_id).order_by(Image.id)).all()

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("classes.txt", "\n".join(c.name for c in classes) + "\n")
        for img in images:
            src = UPLOAD_DIR / str(project_id) / img.stored_name
            if not src.exists():
                continue
            ext = Path(img.stored_name).suffix
            img_arc = f"images/{img.id}{ext}"
            zf.write(src, img_arc)
            anns = session.exec(select(Annotation).where(Annotation.image_id == img.id)).all()
            lines = []
            for a in anns:
                cls = session.get(ProjectClass, a.class_id)
                if cls is None:
                    continue
                lines.append(f"{cls.ord} {a.x:.6f} {a.y:.6f} {a.w:.6f} {a.h:.6f}")
            label_arc = f"labels/{img.id}.txt"
            zf.writestr(label_arc, "\n".join(lines) + ("\n" if lines else ""))
    buf.seek(0)
    filename = f"{project.name.replace(' ', '_')}_yolo.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
