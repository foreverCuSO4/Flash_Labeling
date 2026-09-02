# Flash Labeling

Collaborative YOLO annotation platform — bounding boxes **and** pose keypoints. Python-first, single Docker image.

## Stack

- **Backend**: FastAPI + SQLite (SQLModel)
- **Frontend**: Vanilla JS + Canvas, served by FastAPI (no build chain)
- **Auth**: Cookie session (PBKDF2 password hashing, itsdangerous tokens)
- **Design**: [DESIGN.md](DESIGN.md) — SpaceX-inspired black/white system
- **Fonts**: English in Garamond (bundled EB Garamond, OFL), Chinese in 华文中宋 / STZhongsong (system font; falls back to STSong → SimSun → serif when not installed)

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
- Projects with custom classes; each class carries a semantic **description** shown to annotators
- **Project settings page**: class management, Markdown **annotation guidelines**, pose keypoint/skeleton config — owner edits, annotators read
- **Two annotation modes** per project:
  - `detection` — bounding boxes
  - `pose` — box + keypoints (visibility 0/1/2), exported as YOLO pose format
- Multi-user claiming: images are read-only until claimed; batch-claim N images at once from the project page; a claim is exclusive and auto-expires after 24h if the image is still unlabeled; "My Claims" tab to review/release your claims; per-member stats (labeled / currently claiming)
- All projects are visible to every registered user; guests can browse images and annotations read-only, and join any project from its page to start claiming and annotating
- Canvas: draw, select, delete; drag keypoints; keyboard shortcuts
- YOLO export: zip with `images/`, `labels/`, `classes.txt`, `data.yaml` (includes `kpt_shape` for pose)
- Lightweight DB migrations on startup (old databases keep working)

## Pose Annotation Workflow

1. Create a project with mode **Pose**, define keypoints (order matters — it's the YOLO order) and skeleton edges
2. On the canvas: drag a box around the instance
3. Click to place each keypoint in order (sidebar shows which one is next)
4. `V` toggles the next keypoint's visibility: 2 visible → 1 occluded → 0 not labeled
5. Drag placed keypoints to adjust; `Delete` removes the selected instance
6. `S` saves

## Keyboard Shortcuts (Annotate Page)

| Key | Action |
|-----|--------|
| 1–8 | Select class |
| S | Save annotations |
| V | Toggle keypoint visibility (pose mode, while placing) |
| Delete / Backspace | Delete selected box/instance |
| Escape | Deselect / cancel placement |
