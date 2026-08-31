# Flash Labeling

Collaborative YOLO bounding-box annotation platform. Python-first, single Docker image.

## Stack

- **Backend**: FastAPI + SQLite (SQLModel)
- **Frontend**: Vanilla JS + Canvas, served by FastAPI (no build chain)
- **Auth**: Cookie session (PBKDF2 password hashing, itsdangerous tokens)
- **Design**: [DESIGN.md](DESIGN.md) — SpaceX-inspired black/white system

## Quick Start

### Docker (recommended)

```bash
docker build -t flash-labeling .
docker run -p 8000:8000 -v fl_data:/app/data flash-labeling
```

Open http://localhost:8000

### Local Dev

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest httpx   # dev deps

uvicorn app.main:app --reload --port 8000
```

## Test

```bash
pytest tests/ -v                              # unit + integration tests
python scripts/smoke_test.py                  # E2E smoke (needs running server)
python scripts/smoke_test.py http://host:port # against remote
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `./data` | SQLite DB + uploaded images |
| `SECRET_KEY` | `dev-secret-...` | Session signing key — **set in production** |

## Features

- User registration / login (cookie session)
- Projects with custom YOLO classes
- Multi-user: claim images to prevent duplicate work, 30-min claim expiry
- Canvas annotation: draw, select, delete boxes; keyboard shortcuts
- YOLO export: zip with `images/`, `labels/`, `classes.txt`
- Project member management (owner adds/removes annotators)

## Keyboard Shortcuts (Annotate Page)

| Key | Action |
|-----|--------|
| 1–8 | Select class |
| S | Save annotations |
| Delete / Backspace | Delete selected box |
| Escape | Deselect |
