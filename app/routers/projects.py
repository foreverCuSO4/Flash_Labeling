import json
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, func, select

from ..config import DEFAULT_MODEL_TAG, MODEL_DIR
from ..db import get_session
from ..models import Annotation, Image, Project, ProjectClass, ProjectMember, ProjectModel, User
from ..security import current_user, get_membership, require_member, require_owner, require_viewer
from .images import claim_expired

router = APIRouter(prefix="/api/projects", tags=["projects"])

VALID_MODES = {"detection", "pose", "segment"}


class ProjectIn(BaseModel):
    name: str
    classes: list[str] = []
    mode: str = "detection"
    keypoints: list[str] = []
    skeleton: list[list[int]] = []


class ProjectPatch(BaseModel):
    name: str | None = None
    guidelines: str | None = None
    keypoints: list[str] | None = None
    skeleton: list[list[int]] | None = None


class ModelSelectIn(BaseModel):
    model_id: int | None = None


class ClassIn(BaseModel):
    name: str
    description: str = ""


class ClassPatch(BaseModel):
    name: str | None = None
    description: str | None = None


class MemberIn(BaseModel):
    email: str


class YamlTextIn(BaseModel):
    """YAML text sent by clients that cannot stream multipart file bodies."""

    filename: str = "dataset.yaml"
    content: str
    name: str = ""
    mode: str = ""


def project_out(session: Session, project: Project, membership: ProjectMember | None) -> dict:
    classes = session.exec(
        select(ProjectClass).where(ProjectClass.project_id == project.id).order_by(ProjectClass.ord)
    ).all()
    img_count = session.exec(
        select(func.count(Image.id)).where(Image.project_id == project.id)
    ).one()
    labeled_count = session.exec(
        select(func.count(Image.id)).where(Image.project_id == project.id, Image.status == "labeled")
    ).one()
    models = session.exec(
        select(ProjectModel).where(ProjectModel.project_id == project.id)
        .order_by(ProjectModel.id)
    ).all()
    selected = next((m for m in models if m.id == project.model_id), None)
    return {
        "id": project.id,
        "name": project.name,
        "owner_id": project.owner_id,
        "role": membership.role if membership else None,
        "mode": project.mode,
        "guidelines": project.guidelines,
        "keypoints": json.loads(project.keypoints or "[]"),
        "skeleton": json.loads(project.skeleton or "[]"),
        "model": {
            "id": selected.id if selected else None,
            "name": selected.name if selected else DEFAULT_MODEL_TAG,
            "builtin": selected is None,
        },
        "models": [{
            "id": m.id, "name": m.name, "filename": m.original_name,
            "size_bytes": m.size_bytes,
        } for m in models],
        "classes": [{"id": c.id, "name": c.name, "description": c.description, "ord": c.ord} for c in classes],
        "image_count": img_count,
        "labeled_count": labeled_count,
        "created_at": project.created_at.isoformat(),
    }


@router.get("")
def list_projects(user: User = Depends(current_user), session: Session = Depends(get_session)):
    # All projects are visible to every logged-in user; role marks membership.
    projects = session.exec(select(Project).order_by(Project.id)).all()
    memberships = {m.project_id: m for m in session.exec(
        select(ProjectMember).where(ProjectMember.user_id == user.id)
    ).all()}
    return [project_out(session, p, memberships.get(p.id)) for p in projects]


@router.post("")
def create_project(body: ProjectIn, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if body.mode not in VALID_MODES:
        raise HTTPException(400, f"mode must be one of {sorted(VALID_MODES)}")
    keypoints = [k.strip() for k in body.keypoints if k.strip()]
    if body.mode == "pose" and not keypoints:
        raise HTTPException(400, "pose project requires at least one keypoint")
    for edge in body.skeleton:
        if len(edge) != 2 or not (0 <= edge[0] < len(keypoints)) or not (0 <= edge[1] < len(keypoints)):
            raise HTTPException(400, f"skeleton edge {edge} out of keypoint range")
    project = Project(
        name=body.name, owner_id=user.id, mode=body.mode,
        keypoints=json.dumps(keypoints), skeleton=json.dumps(body.skeleton),
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
    for i, name in enumerate(body.classes):
        name = name.strip()
        if name:
            session.add(ProjectClass(project_id=project.id, name=name, ord=i))
    session.commit()
    membership = get_membership(session, project.id, user.id)
    return project_out(session, project, membership)


def _infer_mode_from_yaml(data: dict) -> str:
    task = str(data.get("task") or "").lower()
    fmt = str(data.get("annotation_format") or "").lower()
    # kpt_shape means the labels are (possibly bbox-less) keypoint sets trained
    # with a pose model — that signal beats a polygon-style annotation_format.
    if data.get("kpt_shape") or "pose" in task:
        return "pose"
    if "quad" in task or "seg" in task:
        return "segment"
    if "x3" in fmt or "y3" in fmt:  # polygon point list beyond a plain bbox
        return "segment"
    return "detection"


def _create_from_yaml_data(
    session: Session,
    user: User,
    data,
    *,
    fallback_name: str,
    name_override: str | None,
    mode_override: str | None,
) -> dict:
    """Create a project skeleton (name, classes, mode) from parsed dataset YAML.

    Images/labels are not imported — only the project structure.
    """
    if not isinstance(data, dict):
        raise HTTPException(400, "dataset YAML must be a mapping")

    names = data.get("names")
    if isinstance(names, dict):  # {0: cat, 1: dog} — order by class id
        try:
            names = [names[k] for k in sorted(names, key=int)]
        except (ValueError, KeyError):
            raise HTTPException(400, "names mapping must use integer class ids")
    if not isinstance(names, list) or not names:
        raise HTTPException(400, "dataset YAML has no class names")
    classes = [str(n).strip() for n in names]
    if not all(classes):
        raise HTTPException(400, "class names must be non-empty")

    mode = mode_override or _infer_mode_from_yaml(data)
    if mode not in VALID_MODES:
        raise HTTPException(400, f"mode must be one of {sorted(VALID_MODES)}")
    name = (name_override or "").strip() or str(data.get("dataset_name") or data.get("name") or fallback_name)

    keypoints: list[str] = []
    if mode == "pose":
        kpt = data.get("kpt_shape")
        n = kpt[0] if isinstance(kpt, (list, tuple)) and kpt and isinstance(kpt[0], int) else 0
        if n <= 0:
            raise HTTPException(400, "pose mode requires kpt_shape: [n, dim] in the YAML")
        keypoints = [f"kp_{i}" for i in range(n)]

    project = Project(
        name=name, owner_id=user.id, mode=mode,
        keypoints=json.dumps(keypoints), skeleton="[]",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
    for i, cname in enumerate(classes):
        session.add(ProjectClass(project_id=project.id, name=cname, ord=i))
    session.commit()
    membership = get_membership(session, project.id, user.id)
    return project_out(session, project, membership)


def _parse_yaml_bytes(raw: bytes) -> dict:
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"cannot parse YAML: {e}")


_MAX_YAML_BYTES = 1 << 20  # 1 MiB is generous for a dataset.yaml


def _create_project_from_yaml_raw(
    raw: bytes,
    filename: str,
    *,
    name: str,
    mode: str,
    user: User,
    session: Session,
) -> dict:
    filename = filename or "dataset.yaml"
    if Path(filename).suffix.lower() not in (".yaml", ".yml"):
        raise HTTPException(400, f"not a YAML file: {filename}")
    if not raw:
        raise HTTPException(400, "empty YAML file; choose a non-empty .yaml or .yml file")
    if len(raw) > _MAX_YAML_BYTES:
        raise HTTPException(
            413,
            f"YAML file is too large ({len(raw)} bytes; limit is {_MAX_YAML_BYTES} bytes)",
        )
    data = _parse_yaml_bytes(raw)
    return _create_from_yaml_data(
        session, user, data,
        fallback_name=Path(filename).stem,
        name_override=name or None,
        mode_override=mode or None,
    )


@router.post("/from-yaml")
async def create_project_from_yaml(
    file: UploadFile,
    name: str = Form(""),
    mode: str = Form(""),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Create a project skeleton from a dataset.yaml uploaded from the browser."""
    raw = await file.read(_MAX_YAML_BYTES + 1)
    return _create_project_from_yaml_raw(
        raw, file.filename or "dataset.yaml", name=name, mode=mode,
        user=user, session=session,
    )


@router.post("/from-yaml-text")
def create_project_from_yaml_text(
    body: YamlTextIn,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Create a project from YAML text read by the browser.

    This avoids multipart streaming problems seen with some browser/network
    combinations. The original multipart endpoint remains available for API
    clients and backwards compatibility.
    """
    try:
        raw = body.content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HTTPException(400, f"YAML text is not valid UTF-8: {exc}")
    return _create_project_from_yaml_raw(
        raw, body.filename, name=body.name, mode=body.mode,
        user=user, session=session,
    )


@router.get("/{project_id}")
def get_project(project_id: int, deps=Depends(require_viewer), session: Session = Depends(get_session)):
    project, membership = deps
    return project_out(session, project, membership)


@router.post("/{project_id}/join")
def join_project(project_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    if get_membership(session, project_id, user.id):
        raise HTTPException(409, "already a member")
    membership = ProjectMember(project_id=project_id, user_id=user.id, role="annotator")
    session.add(membership)
    session.commit()
    return project_out(session, project, membership)


@router.patch("/{project_id}")
def update_project(project_id: int, body: ProjectPatch, deps=Depends(require_owner), session: Session = Depends(get_session)):
    project, membership = deps
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name cannot be empty")
        project.name = name
    if body.guidelines is not None:
        project.guidelines = body.guidelines
    if body.keypoints is not None or body.skeleton is not None:
        if project.mode != "pose":
            raise HTTPException(400, "keypoints/skeleton only apply to pose projects")
        new_keypoints = [k.strip() for k in (body.keypoints if body.keypoints is not None else json.loads(project.keypoints or "[]")) if k.strip()]
        new_skeleton = body.skeleton if body.skeleton is not None else json.loads(project.skeleton or "[]")
        if not new_keypoints:
            raise HTTPException(400, "pose project requires at least one keypoint")
        # Refuse to change the keypoint count once pose annotations exist — existing
        # rows would silently mismatch the new definition.
        pose_ann_count = session.exec(
            select(func.count(Annotation.id))
            .join(Image, Annotation.image_id == Image.id)
            .where(Image.project_id == project_id, Annotation.keypoints.is_not(None))
        ).one()
        old_count = len(json.loads(project.keypoints or "[]"))
        if pose_ann_count and len(new_keypoints) != old_count:
            raise HTTPException(409, "cannot change keypoint count while pose annotations exist")
        for edge in new_skeleton:
            if len(edge) != 2 or not (0 <= edge[0] < len(new_keypoints)) or not (0 <= edge[1] < len(new_keypoints)):
                raise HTTPException(400, f"skeleton edge {edge} out of keypoint range")
        project.keypoints = json.dumps(new_keypoints)
        project.skeleton = json.dumps(new_skeleton)
    session.add(project)
    session.commit()
    session.refresh(project)
    return project_out(session, project, membership)


@router.patch("/{project_id}/model")
def update_project_model(project_id: int, body: ModelSelectIn,
                         deps=Depends(require_owner), session: Session = Depends(get_session)):
    project, membership = deps
    if body.model_id is not None:
        model = session.get(ProjectModel, body.model_id)
        if model is None or model.project_id != project_id:
            raise HTTPException(400, "model does not belong to this project")
    project.model_id = body.model_id
    session.add(project)
    session.commit()
    session.refresh(project)
    return project_out(session, project, membership)


_MAX_MODEL_BYTES = 2 * 1024 ** 3


@router.post("/{project_id}/models")
async def upload_project_model(project_id: int, file: UploadFile,
                               deps=Depends(require_owner),
                               user: User = Depends(current_user),
                               session: Session = Depends(get_session)):
    project, membership = deps
    filename = Path(file.filename or "model.onnx").name
    if Path(filename).suffix.lower() != ".onnx":
        raise HTTPException(400, "only .onnx models are supported")
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model = ProjectModel(
        project_id=project_id,
        name=Path(filename).stem[:120] or "uploaded-model",
        original_name=filename,
        stored_name="",
        created_by=user.id,
    )
    session.add(model)
    session.commit()
    session.refresh(model)
    stored_name = f"{model.id}.onnx"
    dest = MODEL_DIR / stored_name
    size = 0
    try:
        with open(dest, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_MODEL_BYTES:
                    raise HTTPException(413, "model file is too large")
                fh.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        session.delete(model)
        session.commit()
        raise
    if size == 0:
        dest.unlink(missing_ok=True)
        session.delete(model)
        session.commit()
        raise HTTPException(400, "empty model file")
    model.stored_name = stored_name
    model.size_bytes = size
    project.model_id = model.id
    session.add(model)
    session.add(project)
    session.commit()
    session.refresh(project)
    return project_out(session, project, membership)


@router.delete("/{project_id}/models/{model_id}")
def delete_project_model(project_id: int, model_id: int,
                         deps=Depends(require_owner), session: Session = Depends(get_session)):
    project, membership = deps
    model = session.get(ProjectModel, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(404, "model not found")
    if project.model_id == model_id:
        raise HTTPException(409, "cannot delete the active model")
    (MODEL_DIR / model.stored_name).unlink(missing_ok=True)
    session.delete(model)
    session.commit()
    return project_out(session, project, membership)


@router.delete("/{project_id}")
def delete_project(project_id: int, deps=Depends(require_owner), session: Session = Depends(get_session)):
    project, _ = deps
    for model in (ProjectClass, ProjectMember, ProjectModel, Image):
        for row in session.exec(select(model).where(model.project_id == project_id)).all():
            if isinstance(row, ProjectModel):
                (MODEL_DIR / row.stored_name).unlink(missing_ok=True)
            session.delete(row)
    session.delete(project)
    session.commit()
    return {"ok": True}


@router.post("/{project_id}/classes")
def add_class(project_id: int, body: ClassIn, deps=Depends(require_owner), session: Session = Depends(get_session)):
    project, _ = deps
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "class name required")
    existing = session.exec(
        select(ProjectClass).where(ProjectClass.project_id == project_id, ProjectClass.name == name)
    ).first()
    if existing:
        raise HTTPException(409, "class already exists")
    max_ord = session.exec(
        select(func.max(ProjectClass.ord)).where(ProjectClass.project_id == project_id)
    ).one()
    cls = ProjectClass(project_id=project_id, name=name, description=body.description.strip(),
                       ord=(max_ord or 0) + 1 if max_ord is not None else 0)
    session.add(cls)
    session.commit()
    session.refresh(cls)
    return {"id": cls.id, "name": cls.name, "description": cls.description, "ord": cls.ord}


def _get_class(session: Session, project_id: int, class_id: int) -> ProjectClass:
    cls = session.get(ProjectClass, class_id)
    if cls is None or cls.project_id != project_id:
        raise HTTPException(404, "class not found")
    return cls


@router.patch("/{project_id}/classes/{class_id}")
def update_class(project_id: int, class_id: int, body: ClassPatch,
                 deps=Depends(require_owner), session: Session = Depends(get_session)):
    cls = _get_class(session, project_id, class_id)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "class name required")
        dup = session.exec(
            select(ProjectClass).where(
                ProjectClass.project_id == project_id,
                ProjectClass.name == name,
                ProjectClass.id != class_id,
            )
        ).first()
        if dup:
            raise HTTPException(409, "class already exists")
        cls.name = name
    if body.description is not None:
        cls.description = body.description.strip()
    session.add(cls)
    session.commit()
    session.refresh(cls)
    return {"id": cls.id, "name": cls.name, "description": cls.description, "ord": cls.ord}


@router.delete("/{project_id}/classes/{class_id}")
def delete_class(project_id: int, class_id: int, deps=Depends(require_owner), session: Session = Depends(get_session)):
    cls = _get_class(session, project_id, class_id)
    ref_count = session.exec(
        select(func.count(Annotation.id)).where(Annotation.class_id == class_id)
    ).one()
    if ref_count:
        raise HTTPException(409, f"class is referenced by {ref_count} annotation(s)")
    session.delete(cls)
    session.commit()
    # Re-pack ord so exported class ids stay contiguous.
    remaining = session.exec(
        select(ProjectClass).where(ProjectClass.project_id == project_id).order_by(ProjectClass.ord)
    ).all()
    for i, c in enumerate(remaining):
        c.ord = i
        session.add(c)
    session.commit()
    return {"ok": True}


@router.get("/{project_id}/members")
def list_members(project_id: int, deps=Depends(require_viewer), session: Session = Depends(get_session)):
    members = session.exec(select(ProjectMember).where(ProjectMember.project_id == project_id)).all()
    out = []
    for m in members:
        u = session.get(User, m.user_id)
        if u:
            out.append({"user_id": u.id, "email": u.email, "name": u.name, "role": m.role})
    return out


@router.get("/{project_id}/stats")
def project_stats(project_id: int, deps=Depends(require_viewer), session: Session = Depends(get_session)):
    """Per-member progress: images labeled by each user and their active claims."""
    members = session.exec(select(ProjectMember).where(ProjectMember.project_id == project_id)).all()
    images = session.exec(select(Image).where(Image.project_id == project_id)).all()
    labeled_by_user: dict[int, int] = {}
    claimed_by_user: dict[int, int] = {}
    for img in images:
        if img.status == "labeled" and img.labeled_by is not None:
            labeled_by_user[img.labeled_by] = labeled_by_user.get(img.labeled_by, 0) + 1
        if img.status == "unlabeled" and img.claimed_by is not None and not claim_expired(img):
            claimed_by_user[img.claimed_by] = claimed_by_user.get(img.claimed_by, 0) + 1
    out = []
    for m in members:
        u = session.get(User, m.user_id)
        if u:
            out.append({
                "user_id": u.id, "email": u.email, "name": u.name, "role": m.role,
                "labeled_count": labeled_by_user.get(u.id, 0),
                "claimed_count": claimed_by_user.get(u.id, 0),
            })
    return out


@router.post("/{project_id}/members")
def add_member(project_id: int, body: MemberIn, deps=Depends(require_owner), session: Session = Depends(get_session)):
    target = session.exec(select(User).where(User.email == body.email)).first()
    if target is None:
        raise HTTPException(404, "user not found")
    if get_membership(session, project_id, target.id):
        raise HTTPException(409, "already a member")
    session.add(ProjectMember(project_id=project_id, user_id=target.id, role="annotator"))
    session.commit()
    return {"user_id": target.id, "email": target.email, "name": target.name, "role": "annotator"}


@router.delete("/{project_id}/members/{user_id}")
def remove_member(project_id: int, user_id: int, deps=Depends(require_owner), session: Session = Depends(get_session)):
    project, _ = deps
    if user_id == project.owner_id:
        raise HTTPException(400, "cannot remove owner")
    m = get_membership(session, project_id, user_id)
    if m is None:
        raise HTTPException(404, "member not found")
    session.delete(m)
    session.commit()
    return {"ok": True}
