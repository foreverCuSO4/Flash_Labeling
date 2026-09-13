# Flash Labeling

Collaborative YOLO annotation platform — bounding boxes, pose keypoints, and segmentation polygons. Python-first, single Docker image.

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

### Domain sub-path deployment

The app can be published below a domain path such as
`https://example.com/flash_labeling/`. Start FastAPI with the matching
`ROOT_PATH` and keep the app bound to localhost:

```bash
ROOT_PATH=/flash_labeling uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then install [deploy/nginx/flash_labeling.conf](deploy/nginx/flash_labeling.conf)
as an HTTP server configuration (or copy its `location` blocks into the
existing HTTPS server). It already declares `server_name 60.165.239.63;`.
The proxy strips `/flash_labeling` before forwarding to FastAPI, while the
frontend keeps that prefix for API calls, navigation, images, avatars, and
resumable upload chunks. The same files still work when served directly at `/`.

For Docker, pass the same setting and bind the published port locally:

```bash
docker run -p 127.0.0.1:8000:8000 \
  -e ROOT_PATH=/flash_labeling \
  -v fl_data:/app/data flash-labeling
```

The existing [run_docker.sh](run_docker.sh) now has the production sub-path
values fixed in the script, so use it without arguments:

```bash
./run_docker.sh
```

The script currently fixes `INFER_URL` to `http://172.17.0.1:8787`, which is the
host gateway used by this server. If the Docker bridge gateway differs, edit
the `INFER_URL` constant in [run_docker.sh](run_docker.sh); the container's
`127.0.0.1` is its own loopback.

```bash
./run_docker.sh
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
- **Three annotation modes** per project:
  - `detection` — bounding boxes
  - `pose` — box + keypoints (visibility 0/1/2), exported as YOLO pose format
  - `segment` — click-point polygons (vertex drag editing), exported as YOLO segment format
- Create projects manually or **by uploading a dataset.yaml** — classes and mode are inferred (`kpt_shape` → pose with auto-named keypoints; polygon-style `annotation_format`/`task` → segment; otherwise detection); images are not imported
- Multi-user claiming: images are read-only until claimed; batch-claim N images at once from the project page; a claim is exclusive and auto-expires after 24h if the image is still unlabeled; "My Claims" tab to review/release your claims; per-member stats (labeled / currently claiming)
- **Uploads live on a separate page** (`/upload.html?project=…`) reached from the project header — images in bulk, videos via chunked resumable upload; the project page stays focused on labeling
- **Dataset management (owner)**: a Manage mode on the project page lets you select images (all / clear / per-card checkboxes) and bulk-delete them with their annotations
- All projects are visible to every registered user; guests can browse images and annotations read-only, and join any project from its page to start claiming and annotating
- Canvas: draw, select, delete; drag keypoints; keyboard shortcuts
- **Video import**: upload videos (e.g. 100fps footage) on the upload page; regular extraction samples at a fixed, configurable interval (default 0.2s); extracted frames land in the project as regular images; original videos are kept under `data/videos/`
- Extraction is **parallel**: each video is split into frame ranges decoded on up to `VIDEO_EXTRACT_WORKERS` threads (default 4) while jobs run one at a time; a single video runs several times faster than realtime decode would
- **Model auto-scan (optional)**: with the [NPU inference backend](docs/inference_backend.md) running, video import offers a *Model auto-scan* mode — the whole video is scanned by the model at a low confidence floor (default 0.2), hit frames are dilated ±3s and unioned into "annotation-worthy" windows, and only windows are sampled (default 10fps); per-frame detections are cached (`data/cache/det/`) for brush annotation
- **Brush annotation (auto-assist)**: on the annotate page the brush opens by default; click inside the circle and a detection under the cursor (model floor 0.05) becomes an annotation with the currently selected class — geometry is snapped, semantics stay with the annotator; scroll on the canvas changes brush size, and `B` closes brush mode
- Uploads are **chunked, resumable and parallel** (16 MiB parts, 4 in flight, out-of-order writes with server-tracked ranges): progress is per-byte visible, a dropped link or page reload just resumes, retries are idempotent; unfinished uploads are swept after 24h
- YOLO export: zip with `images/`, `labels/`, `classes.txt`, `data.yaml` (includes `kpt_shape` for pose)
- Lightweight DB migrations on startup (old databases keep working)

## Pose Annotation Workflow

1. Create a project with mode **Pose**, define keypoints (order matters — it's the YOLO order) and skeleton edges
2. On the canvas: drag a box around the instance — **or** just click the keypoints in order on empty canvas (keypoints-first; the box is derived automatically, e.g. 4 ordered corners)
3. Click to place each keypoint in order (sidebar shows which one is next)
4. `V` toggles the next keypoint's visibility: 2 visible → 1 occluded → 0 not labeled
5. Drag placed keypoints to adjust; `Delete` removes the selected instance
6. Annotations save automatically after each completed action; `S` can still save manually

## Keyboard Shortcuts (Annotate Page)

| Key | Action |
|-----|--------|
| 1–8 | Select class |
| B | Close brush mode (it opens by default) |
| Mouse wheel | Change brush size while brush mode is active |
| Left / Right | Previous / next image |
| Right click | Erase the annotation under the cursor |
| S | Save annotations |
| Enter | Close polygon (segment mode, while drawing) |
| V | Toggle keypoint visibility (pose mode, while placing) |
| Delete / Backspace | Delete selected box/instance, or cancel polygon draft |
| Escape | Deselect / cancel placement or polygon draft |

### Kubernetes deployment (canonical)

This server uses the existing Kubernetes `ingress-nginx` and Ascend device
plugin. The production deployment is a single application Pod plus a single
Ascend inference Pod. Application state stays in the node-local data directory
and the inference Pod mounts the node's CANN 8.2.RC1 installation and the OM
files from `data/om_models`.

After a kubeconfig is available and the current user has permission to import
images into the Kubernetes containerd namespace, rebuild and start the complete
stack with no arguments:

```bash
./rebuild.sh
./start.sh
```

The familiar `build_docker.sh` and `run_docker.sh` names are no-argument
wrappers for these Kubernetes commands. The application intentionally remains
at one replica because SQLite and uploaded files use node-local storage.

The Kubernetes Ingress exposes:

```text
http://60.165.239.63/flash_labeling/
```

The standalone `deploy/nginx/flash_labeling.conf` remains available as a
fallback for a host-level Nginx installation; it is not consumed by the
Kubernetes ingress controller.

For a first-time setup on this server, run the following three commands in
order. `setup_k8s.sh` only copies the cluster access file into your user
account; it does not change Kubernetes resources.

```bash
./setup_k8s.sh
./rebuild.sh
./start.sh
```

The Kubernetes startup script creates a persistent `flash-labeling-secrets`
Secret with a generated session signing key on first start and reuses it on
later restarts.
