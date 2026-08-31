from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, func, select

from ..db import get_session
from ..models import Image, Project, ProjectClass, ProjectMember, User
from ..security import current_user, get_membership, require_member, require_owner

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str
    classes: list[str] = []


class ClassIn(BaseModel):
    name: str


class MemberIn(BaseModel):
    email: str


def project_out(session: Session, project: Project, membership: ProjectMember) -> dict:
    classes = session.exec(
        select(ProjectClass).where(ProjectClass.project_id == project.id).order_by(ProjectClass.ord)
    ).all()
    img_count = session.exec(
        select(func.count(Image.id)).where(Image.project_id == project.id)
    ).one()
    labeled_count = session.exec(
        select(func.count(Image.id)).where(Image.project_id == project.id, Image.status == "labeled")
    ).one()
    return {
        "id": project.id,
        "name": project.name,
        "owner_id": project.owner_id,
        "role": membership.role,
        "classes": [{"id": c.id, "name": c.name, "ord": c.ord} for c in classes],
        "image_count": img_count,
        "labeled_count": labeled_count,
        "created_at": project.created_at.isoformat(),
    }


@router.get("")
def list_projects(user: User = Depends(current_user), session: Session = Depends(get_session)):
    memberships = session.exec(select(ProjectMember).where(ProjectMember.user_id == user.id)).all()
    out = []
    for m in memberships:
        project = session.get(Project, m.project_id)
        if project:
            out.append(project_out(session, project, m))
    return out


@router.post("")
def create_project(body: ProjectIn, user: User = Depends(current_user), session: Session = Depends(get_session)):
    project = Project(name=body.name, owner_id=user.id)
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


@router.get("/{project_id}")
def get_project(project_id: int, deps=Depends(require_member), session: Session = Depends(get_session)):
    project, membership = deps
    return project_out(session, project, membership)


@router.delete("/{project_id}")
def delete_project(project_id: int, deps=Depends(require_owner), session: Session = Depends(get_session)):
    project, _ = deps
    for model in (ProjectClass, ProjectMember, Image):
        for row in session.exec(select(model).where(model.project_id == project_id)).all():
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
    cls = ProjectClass(project_id=project_id, name=name, ord=(max_ord or 0) + 1 if max_ord is not None else 0)
    session.add(cls)
    session.commit()
    session.refresh(cls)
    return {"id": cls.id, "name": cls.name, "ord": cls.ord}


@router.get("/{project_id}/members")
def list_members(project_id: int, deps=Depends(require_member), session: Session = Depends(get_session)):
    members = session.exec(select(ProjectMember).where(ProjectMember.project_id == project_id)).all()
    out = []
    for m in members:
        u = session.get(User, m.user_id)
        if u:
            out.append({"user_id": u.id, "email": u.email, "name": u.name, "role": m.role})
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
