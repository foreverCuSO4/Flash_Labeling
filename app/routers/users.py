import uuid
from io import BytesIO
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from PIL import Image as PILImage
from sqlmodel import Session, select

from ..config import AVATAR_DIR
from ..db import get_session
from ..models import User
from ..security import current_user

router = APIRouter(prefix="/api/users", tags=["users"])

MAX_AVATAR_BYTES = 5 * 1024 * 1024

# Same palette as .class-color-* in style.css
_PALETTE = ["#ff6b6b", "#51cf66", "#339af0", "#ffd43b", "#cc5de8", "#ff922b", "#20c997", "#f783ac"]

_SERIF_LATIN = "'EB Garamond', Georgia, 'Times New Roman', serif"
_SERIF_CJK = "'Songti SC', 'STSong', 'SimSun', serif"


def user_out(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "avatar_url": f"/api/users/{user.id}/avatar",
        "created_at": user.created_at.isoformat(),
    }


def _monogram(name: str) -> tuple[str, str]:
    """Pick the avatar glyph and a matching serif stack.

    CJK names use the surname (first character); Latin names use a two-letter
    monogram from the first two words, falling back to a single initial.
    """
    name = (name or "").strip()
    if not name:
        return "?", _SERIF_LATIN
    if "一" <= name[0] <= "鿿":  # CJK Unified Ideographs
        return name[0], _SERIF_CJK
    words = [w for w in name.replace("_", " ").split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper(), _SERIF_LATIN
    return name[0].upper(), _SERIF_LATIN


def _default_avatar(user: User) -> Response:
    """Generated fallback: hairline ring, serif monogram, and a short accent arc
    seeded by user id so identical monograms still look distinct."""
    glyph, font_stack = _monogram(user.name)
    color = _PALETTE[user.id % len(_PALETTE)]
    rotation = (user.id * 47) % 360
    size = 30 if len(glyph) == 1 else 24
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<circle cx="32" cy="32" r="32" fill="#000000"/>'
        '<circle cx="32" cy="32" r="29.5" fill="none" stroke="#f0f0fa" stroke-opacity="0.55" stroke-width="1"/>'
        f'<circle cx="32" cy="32" r="29.5" fill="none" stroke="{color}" stroke-width="2.5"'
        f' stroke-linecap="round" stroke-dasharray="26 159.4" transform="rotate({rotation} 32 32)"/>'
        f'<text x="32" y="33" font-family="{font_stack}" font-size="{size}"'
        f' text-anchor="middle" dominant-baseline="central" fill="#f0f0fa">{escape(glyph)}</text>'
        "</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml")


@router.get("")
def list_users(user: User = Depends(current_user), session: Session = Depends(get_session)):
    users = session.exec(select(User).order_by(User.id)).all()
    return [user_out(u) for u in users]


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    raw = file.file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > MAX_AVATAR_BYTES:
        raise HTTPException(400, "avatar too large (max 5 MB)")
    try:
        with PILImage.open(BytesIO(raw)) as im:
            im.verify()
        with PILImage.open(BytesIO(raw)) as im:
            fmt = (im.format or "").lower()
    except Exception:
        raise HTTPException(400, "not a valid image")
    ext = {"jpeg": ".jpg", "png": ".png", "bmp": ".bmp", "webp": ".webp"}.get(fmt)
    if ext is None:
        raise HTTPException(400, "unsupported image type")
    old_path = AVATAR_DIR / user.avatar if user.avatar else None
    stored = f"{uuid.uuid4().hex}{ext}"
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    (AVATAR_DIR / stored).write_bytes(raw)
    if old_path and old_path.exists():
        old_path.unlink()
    user.avatar = stored
    session.add(user)
    session.commit()
    session.refresh(user)
    return user_out(user)


@router.get("/{user_id}/avatar")
def get_avatar(user_id: int, session: Session = Depends(get_session)):
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    if user.avatar:
        path = AVATAR_DIR / user.avatar
        if path.exists():
            return FileResponse(path)
    return _default_avatar(user)
