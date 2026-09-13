"""Auto-labeling: window math, brush hit-test, cache, brush API, auto video job."""
import io
import json
import time

import cv2
import numpy as np
import pytest

from app.autolabel import (
    AutoScanParams,
    ParamsError,
    brush_hit,
    compute_windows,
    frame_has_hit,
    row_box_norm,
    row_class_index,
    row_corners_norm,
    sample_frames,
)
from app.detector import (
    read_cache,
    set_detector_for_tests,
    write_cache,
)


def make_row(x1, y1, x2, y2, score):
    """Fabricate a corner-format row for an axis-aligned quad:
    TL(x1,y1) BL(x1,y2) BR(x2,y2) TR(x2,y1)."""
    row = np.zeros(21, np.float32)
    row[:8] = x1, y1, x1, y2, x2, y2, x2, y1
    row[8] = score   # cls col 0 holds the max score
    return row


def rows(*specs):
    return np.stack([make_row(*s) for s in specs]) if specs \
        else np.empty((0, 21), np.float32)


# ---------------------------------------------------------------------------
# AutoScanParams

class TestAutoScanParams:
    def test_defaults(self):
        p = AutoScanParams.from_dict(None)
        assert (p.conf, p.dilate_s, p.sample_fps) == (0.2, 3.0, 10.0)
        assert p.max_frames == 5000

    def test_roundtrip(self):
        d = {"conf": 0.1, "dilate_s": 5, "sample_fps": 25,
             "scan_stride": 2, "jpeg_quality": 80, "max_frames": 100}
        p = AutoScanParams.from_dict(d)
        assert p.scan_stride == 2 and p.jpeg_quality == 80
        assert p.to_dict()["conf"] == pytest.approx(0.1)

    @pytest.mark.parametrize("bad", [
        {"conf": 0}, {"conf": 1.5}, {"dilate_s": -1}, {"dilate_s": 100},
        {"sample_fps": 0}, {"sample_fps": 100}, {"scan_stride": 0},
        {"scan_stride": 1.5}, {"jpeg_quality": 200}, {"max_frames": 0},
    ])
    def test_invalid(self, bad):
        with pytest.raises(ParamsError):
            AutoScanParams.from_dict(bad)


# ---------------------------------------------------------------------------
# Windows / sampling

class TestWindows:
    def test_dilate_and_merge(self):
        hits = [False] * 100
        hits[10] = True
        hits[12] = True
        hits[50] = True
        w = compute_windows(hits, fps=10.0, dilate_s=1.0)   # ±10 frames
        assert w == [(0, 23), (40, 61)]

    def test_clamp_at_zero(self):
        hits = [True, False, False]
        # pad = 2.0*10 = 20 frames: start clamps to 0, end extends unchecked
        assert compute_windows(hits, fps=10, dilate_s=2.0) == [(0, 21)]

    def test_no_hits(self):
        assert compute_windows([False] * 10, 30.0, 3.0) == []

    def test_grid_sampling(self):
        w = [(0, 100)]
        picks = sample_frames(w, fps=30.0, sample_fps=10.0, max_frames=1000)
        assert picks == [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 33,
                         36, 39, 42, 45, 48, 51, 54, 57, 60, 63, 66,
                         69, 72, 75, 78, 81, 84, 87, 90, 93, 96, 99]

    def test_grid_cap(self):
        picks = sample_frames([(0, 10 ** 9)], 30.0, 10.0, max_frames=5)
        assert picks == [0, 3, 6, 9, 12]


# ---------------------------------------------------------------------------
# Brush hit-test

class TestBrushHit:
    def test_class_index_uses_highest_score(self):
        r = rows((100, 100, 200, 200, 0.3))[0]
        r[8:12] = [0.1, 0.8, 0.2, 0.4]
        r[12:21] = [0.1, 0.2, 0.9, 0.3, 0.4, 0.2, 0.1, 0.3, 0.2]
        assert row_class_index(r) == 11  # prefix 1 × 9 + board type 2

    def test_hit_by_center(self):
        r = rows((100, 100, 200, 200, 0.9))   # center (150,150) in 640x384
        hit = brush_hit(r, 150 / 640, 150 / 384, r_px=20, img_w=640, img_h=384)
        assert hit is not None
        x, y, w, h = row_box_norm(hit)
        assert x == pytest.approx(150 / 640)
        assert w == pytest.approx(100 / 640)
        assert np.allclose(row_corners_norm(hit), [
            [100 / 640, 100 / 384], [100 / 640, 200 / 384],
            [200 / 640, 200 / 384], [200 / 640, 100 / 384],
        ])

    def test_miss_outside_circle(self):
        r = rows((100, 100, 200, 200, 0.9))
        assert brush_hit(r, 0.9, 0.9, r_px=30, img_w=640, img_h=384) is None

    def test_circle_respects_aspect(self):
        # Same normalized center; with a 2:1 image a fixed pixel radius
        # tolerates 2x more normalized displacement on x than on y.
        r = rows((300, 100, 340, 200, 0.9))  # center (320,150)
        # click 20 image-px to the right: (320+20)/640, 150/384 -> inside r=21
        assert brush_hit(r, (320 + 20) / 640, 150 / 384, 21, 640, 384) is not None
        # but 21px displacement (just outside r=20)
        assert brush_hit(r, (320 + 21) / 640, 150 / 384, 20, 640, 384) is None

    def test_best_score_wins(self):
        r = rows((100, 100, 200, 200, 0.3),
                 (110, 110, 210, 210, 0.8))
        hit = brush_hit(r, 160 / 640, 150 / 384, r_px=200, img_w=640, img_h=384)
        assert hit[8] == pytest.approx(0.8)

    def test_empty(self):
        assert brush_hit(np.empty((0, 21), np.float32),
                         0.5, 0.5, 30, 640, 384) is None

    def test_frame_has_hit(self):
        assert frame_has_hit(rows((0, 0, 1, 1, 0.3)), 0.2) is True
        assert frame_has_hit(rows((0, 0, 1, 1, 0.1)), 0.2) is False
        assert frame_has_hit(np.empty((0, 21), np.float32), 0.2) is False


# ---------------------------------------------------------------------------
# Cache round-trip

class TestCache:
    def test_write_read_delete(self, tmp_path):
        r = rows((1, 2, 3, 4, 0.7))
        project_id, stored = tmp_path.name, "abc.jpg"
        write_cache(project_id, stored, r)
        back = read_cache(project_id, stored)
        assert back.shape == (1, 21)
        assert np.allclose(back[0][:8], [1, 2, 1, 4, 3, 4, 3, 2])
        # an unrelated key does not leak
        assert read_cache(project_id, "other.jpg") is None


# ---------------------------------------------------------------------------
# Brush API

@pytest.fixture()
def brush_setup(client, project):
    """Project with classes (conftest.project), an uploaded+claimed image,
    and a fake detector that always yields one box at (100..300, 100..240)."""
    fake_rows = rows((100, 100, 300, 240, 0.9))
    set_detector_for_tests(lambda frames: [fake_rows.copy() for _ in frames])
    # Upload an image directly (plain upload has no cache -> on-demand det)
    img = np.full((384, 640, 3), 40, np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    r = client.post(
        f"/api/projects/{project['id']}/images/upload",
        files={"files": ("f1.jpg", io.BytesIO(buf.tobytes()), "image/jpeg")})
    assert r.status_code == 200
    image = r.json()[0]
    r = client.post(f"/api/projects/{project['id']}/images/{image['id']}/claim")
    assert r.status_code == 200
    yield project, image
    set_detector_for_tests(None)


class TestBrushAPI:
    def test_hit(self, client, brush_setup):
        project, image = brush_setup
        r = client.post(f"/api/images/{image['id']}/brush",
                        json={"x": 200 / 640, "y": 170 / 384, "r": 60})
        assert r.status_code == 200
        body = r.json()
        assert body["cached"] is False      # first call is on-demand
        s = body["suggestion"]
        assert s is not None
        assert s["score"] == pytest.approx(0.9)
        assert s["class_index"] == 0
        assert s["x"] == pytest.approx(200 / 640)
        assert s["w"] == pytest.approx(200 / 640)
        assert np.allclose(s["corners"], [
            [100 / 640, 100 / 384], [100 / 640, 240 / 384],
            [300 / 640, 240 / 384], [300 / 640, 100 / 384],
        ])
        # second call hits the cache written by the first
        r = client.post(f"/api/images/{image['id']}/brush",
                        json={"x": 200 / 640, "y": 170 / 384, "r": 60})
        assert r.json()["cached"] is True

    def test_miss(self, client, brush_setup):
        project, image = brush_setup
        r = client.post(f"/api/images/{image['id']}/brush",
                        json={"x": 0.95, "y": 0.95, "r": 30})
        assert r.json()["suggestion"] is None

    def test_requires_claim(self, client, project, brush_setup):
        _, image = brush_setup
        r = client.post("/api/auth/register", json={
            "email": "mallory@test.com", "name": "M", "password": "x"})
        assert r.status_code == 200
        r = client.post("/api/auth/login", json={
            "email": "mallory@test.com", "password": "x"})
        assert r.status_code == 200
        r = client.post(f"/api/images/{image['id']}/brush",
                        json={"x": 0.5, "y": 0.5, "r": 60})
        assert r.status_code in (403, 404)  # not a member -> hidden


# ---------------------------------------------------------------------------
# Auto video job (end to end with a scripted fake detector)

def make_video(path, fps=10, static_s=2, hitting_s=2, size=(640, 384)):
    """First `static_s` seconds: plain frames. Next `hitting_s`: frames we
    will script the fake detector to detect (tracked by frame index)."""
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"),
                        fps, size)
    if not w.isOpened():
        pytest.skip("no usable video codec in this environment")
    n_static = fps * static_s
    for i in range(n_static + fps * hitting_s):
        frame = np.full((size[1], size[0], 3), 64, np.uint8)
        if i >= n_static:
            cv2.rectangle(frame, (100, 100), (300, 240), (255, 255, 255), -1)
        w.write(frame)
    w.release()
    return n_static   # index of first "hitting" frame


class TestAutoJob:
    @pytest.fixture(autouse=True)
    def _cleanup_video_dir(self, tmp_path):
        """Our video jobs leave files under VIDEO_DIR/<project_id>; later
        chunked-upload tests glob that dir, so restore its pristine state."""
        yield
        from app.config import VIDEO_DIR
        if VIDEO_DIR.exists():
            for d in VIDEO_DIR.iterdir():
                if d.is_dir():
                    for f in d.glob("*.mp4"):
                        f.unlink()

    def test_job(self, client, project, tmp_path):
        video_path = tmp_path / "auto.mp4"
        first_hit = make_video(video_path, fps=10, static_s=2, hitting_s=3)
        hit_rows = rows((100, 100, 300, 240, 0.9))

        def fake_det(frames):
            out = []
            for f in frames:
                # white rectangle present -> hit
                if (f > 200).sum() > 100:
                    out.append(hit_rows.copy())
                else:
                    out.append(np.empty((0, 21), np.float32))
            return out

        set_detector_for_tests(fake_det)

        data = video_path.read_bytes()
        r = client.post(
            f"/api/projects/{project['id']}/videos/upload",
            files={"files": ("auto.mp4", io.BytesIO(data), "video/mp4")},
            data={"params": json.dumps({"auto": {
                "conf": 0.2, "dilate_s": 1.0, "sample_fps": 5.0,
            }})})
        assert r.status_code == 200, r.text
        job = r.json()[0]
        for _ in range(100):
            jr = client.get(f"/api/projects/{project['id']}/videos").json()
            st = next(j for j in jr if j["id"] == job["id"])
            if st["status"] not in ("pending", "running"):
                break
            time.sleep(0.1)
        assert st["status"] == "done", st.get("error")

        imgs = client.get(f"/api/projects/{project['id']}/images").json()
        # hits = frames 20..49; dilated ±10f -> window [10,60), clipped by the
        # 50-frame video; grid 10,12,...,48 -> 20 extractable frames
        assert len(imgs) == 20
        # image_out hides stored_name but puts it in the immutable URL query
        stored_names = [im["url"].split("v=", 1)[1] for im in imgs]
        caches = [read_cache(project["id"], s) for s in stored_names]
        assert all(c is not None for c in caches)
        # frames inside the hit segment have rows; dilation-only frames are
        # empty caches — both are valid, both must be written
        assert any(len(c) > 0 for c in caches)
        assert any(len(c) == 0 for c in caches)
        set_detector_for_tests(None)

    def test_service_down_fails_cleanly(self, client, project, tmp_path):
        """DetectorUnavailable -> job failed with a clear message."""
        from app.detector import DetectorUnavailable
        set_detector_for_tests(
            lambda frames: (_ for _ in ()).throw(
                DetectorUnavailable("inference service not reachable")))
        video_path = tmp_path / "down.mp4"
        make_video(video_path, fps=10, static_s=1, hitting_s=1)
        r = client.post(
            f"/api/projects/{project['id']}/videos/upload",
            files={"files": ("down.mp4", io.BytesIO(video_path.read_bytes()),
                             "video/mp4")},
            data={"params": json.dumps({"auto": {"conf": 0.2}})})
        assert r.status_code == 200
        job = r.json()[0]
        for _ in range(100):
            st = next(j for j in client.get(
                f"/api/projects/{project['id']}/videos").json()
                if j["id"] == job["id"])
            if st["status"] not in ("pending", "running"):
                break
            time.sleep(0.1)
        assert st["status"] == "failed"
        assert "inference" in st["error"]
        set_detector_for_tests(None)
