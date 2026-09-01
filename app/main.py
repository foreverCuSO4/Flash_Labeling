from contextlib import asynccontextmanager
from re import fullmatch

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, UPLOAD_DIR
from .db import init_db
from .routers import annotations, auth, export, images, projects, users

STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="YOLO Labeling Platform", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(images.router)
app.include_router(annotations.router)
app.include_router(export.router)
app.include_router(users.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/images/{image_id}/file")
def image_file(image_id: int):
    from sqlmodel import Session, select
    from .db import engine
    from .models import Image
    with Session(engine) as session:
        img = session.get(Image, image_id)
        if img is None:
            from fastapi import HTTPException
            raise HTTPException(404, "image not found")
        path = UPLOAD_DIR / str(img.project_id) / img.stored_name
        if not path.exists():
            from fastapi import HTTPException
            raise HTTPException(404, "file missing")
        return FileResponse(path)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Force revalidation on every load so redeploys show up without hard refresh.

    Only image file payloads get a long immutable cache — their URLs carry the
    per-upload uuid (`/api/images/<id>/file?v=<stored_name>`), so reused rowids
    can never collide with a stale cache entry. Everything else, including API
    JSON like annotation lists, always revalidates.
    """
    response = await call_next(request)
    if fullmatch(r"/api/images/\d+/file", request.url.path):
        response.headers["Cache-Control"] = "max-age=31536000, immutable"
    else:
        response.headers["Cache-Control"] = "no-cache"
    return response
