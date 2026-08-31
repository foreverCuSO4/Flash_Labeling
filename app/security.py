import hashlib
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlmodel import Session, select

from .config import SECRET_KEY, SESSION_COOKIE, SESSION_MAX_AGE
from .db import get_session
from .models import Project, ProjectMember, User

signer = TimestampSigner(SECRET_KEY)

_PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _, iterations, salt, expected = password_hash.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return secrets.compare_digest(dk.hex(), expected)
    except Exception:
        return False


def make_session_token(user_id: int) -> str:
    return signer.sign(str(user_id)).decode()


def parse_session_token(token: str) -> Optional[int]:
    try:
        value = signer.unsign(token, max_age=SESSION_MAX_AGE).decode()
        return int(value)
    except (BadSignature, SignatureExpired, ValueError):
        return None


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "not authenticated")
    user_id = parse_session_token(token)
    if user_id is None:
        raise HTTPException(401, "session expired or invalid")
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(401, "user not found")
    return user


def get_membership(session: Session, project_id: int, user_id: int) -> Optional[ProjectMember]:
    return session.exec(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    ).first()


def require_member(project_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    membership = get_membership(session, project_id, user.id)
    if membership is None:
        raise HTTPException(403, "not a project member")
    return project, membership


def require_owner(project_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    project, membership = require_member(project_id, user, session)
    if membership.role != "owner":
        raise HTTPException(403, "owner only")
    return project, membership
