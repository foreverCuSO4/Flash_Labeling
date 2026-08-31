from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr
from sqlmodel import Session, select

from ..config import SESSION_COOKIE, SESSION_MAX_AGE
from ..db import get_session
from ..models import User
from ..security import current_user, hash_password, make_session_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    name: str
    password: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


def user_out(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name}


@router.post("/register")
def register(body: RegisterIn, response: Response, session: Session = Depends(get_session)):
    if session.exec(select(User).where(User.email == body.email)).first():
        raise HTTPException(409, "email already registered")
    user = User(email=body.email, name=body.name, password_hash=hash_password(body.password))
    session.add(user)
    session.commit()
    session.refresh(user)
    response.set_cookie(
        SESSION_COOKIE, make_session_token(user.id),
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
    )
    return user_out(user)


@router.post("/login")
def login(body: LoginIn, response: Response, session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.email == body.email)).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "invalid email or password")
    response.set_cookie(
        SESSION_COOKIE, make_session_token(user.id),
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
    )
    return user_out(user)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_out(user)
