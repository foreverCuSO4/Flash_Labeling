"""Video import: motion-adaptive extraction logic + API job lifecycle."""
import math
import time

import cv2
import numpy as np
import pytest

from app.video import (
    Cancelled,
    ExtractParams,
    ParamsError,
    extract_frames,
    interval_for,
)


def make_video(path, fps=30, seconds_static=4, seconds_moving=4, size=(320, 240)):
    """Solid gray segment (static) followed by a fast-moving bright box.

    The box changes ~12% of pixels per frame in the moving segment, putting it
    in the densest default sampling tier; the static segment scores ~0.
    """
    path = str(path)
    for fourcc, ext in ((cv2.VideoWriter_fourcc(*"mp4v"), ".mp4"),
                        (cv2.VideoWriter_fourcc(*"MJPG"), ".avi")):
        p = path if path.endswith(ext) else path.rsplit(".", 1)[0] + ext
        w = cv2.VideoWriter(p, fourcc, fps, size)
        if w.isOpened():
            break
    else:
        pytest.skip("no usable video codec in this environment")
    W, H = size
    box = 120
    n_static = int(fps * seconds_static)
    n_moving = int(fps * seconds_moving)
    for _ in range(n_static):
        w.write(np.full((H, W, 3), 128, np.uint8))
    for i in range(n_moving):
        frame = np.full((H, W, 3), 128, np.uint8)
        x = (i * 40) % (W - box)
        frame[10:10 + box, x:x + box] = 255
        w.write(frame)
    w.release()
    return p, n_static, n_moving


class TestParams:
    def test_defaults(self):
        p = ExtractParams.from_dict(None)
        assert p.tiers[-1][0] == math.inf
        assert p.min_interval < p.max_interval

    def test_from_dict(self):
        p = ExtractParams.from_dict({
            "tiers": [[0.01, 5], [None, 1]],
            "max_frames": 10,
            "jpeg_quality": 80,
        })
        assert p.tiers == [(0.01, 5.0), (math.inf, 1.0)]
        assert p.max_frames == 10
        assert p.jpeg_quality == 80

    def test_roundtrip(self):
        d = ExtractParams().to_dict()
        assert ExtractParams.from_dict(d).tiers == ExtractParams().tiers

    @pytest.mark.parametrize("bad", [
        {"tiers": "nope"},
        {"tiers": []},
        {"tiers": [[0.01, 5]]},            # no catch-all (null ceiling) tier
        {"tiers": [[0.01, 5], [None, 0]]},  # non-positive interval
        {"min_interval": 5, "max_interval": 1},
        {"max_frames": 0},
        {"jpeg_quality": 200},
    ])
    def test_invalid(self, bad):
        with pytest.raises(ParamsError):
            ExtractParams.from_dict(bad)


class TestIntervalFor:
    def test_tier_mapping(self):
        p = ExtractParams()
        assert interval_for(0.0, p) == 10.0
        assert interval_for(0.004, p) == 10.0
        assert interval_for(0.01, p) == 5.0
        assert interval_for(0.05, p) == 1.0
        assert interval_for(0.5, p) == 0.2

    def test_clamped_to_bounds(self):
        p = ExtractParams(min_interval=2.0, max_interval=8.0)
        assert interval_for(0.5, p) == 2.0
        assert interval_for(0.0, p) == 8.0


class TestExtractFrames:
    def test_adaptive_density(self, tmp_path):
        video, n_static, n_moving = make_video(tmp_path / "clip.mp4")
        out = tmp_path / "frames"
        result = extract_frames(video, out, ExtractParams())
        idxs = [f["frame_idx"] for f in result["frames"]]
        static_part = [i for i in idxs if i < n_static]
        moving_part = [i for i in idxs if i >= n_static]
        assert idxs[0] == 0  # first frame is always sampled
        assert 1 <= len(static_part) <= 2  # ~10s interval over a 4s segment
        assert len(moving_part) > 3 * len(static_part)
        for f in result["frames"]:
            img = cv2.imread(str(out / f["stored_name"]))
            assert img is not None and img.shape[:2] == (f["height"], f["width"])

    def test_max_frames_cap(self, tmp_path):
        video, _, _ = make_video(tmp_path / "clip.mp4")
        result = extract_frames(video, tmp_path / "out", ExtractParams(max_frames=3))
        assert result["capped"] is True
        assert len(result["frames"]) == 3

    def test_cancel(self, tmp_path):
        video, _, _ = make_video(tmp_path / "clip.mp4", seconds_static=0, seconds_moving=6)
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 2

        with pytest.raises(Cancelled):
            extract_frames(video, tmp_path / "out", ExtractParams(), should_cancel=cancel)

    def test_progress_callback(self, tmp_path):
        video, n_static, n_moving = make_video(tmp_path / "clip.mp4")
        seen = []
        result = extract_frames(
            video, tmp_path / "out", ExtractParams(),
            on_progress=lambda done, total, extracted: seen.append((done, total, extracted)),
        )
        assert seen and seen[-1][0] == result["total_frames"]
        assert seen[-1][2] == len(result["frames"])

    def test_unopenable_video(self, tmp_path):
        bogus = tmp_path / "bogus.mp4"
        bogus.write_bytes(b"not a video")
        with pytest.raises(RuntimeError):
            extract_frames(bogus, tmp_path / "out", ExtractParams())


class TestApi:
    def _wait_job(self, client, project_id, job_id, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            jobs = client.get(f"/api/projects/{project_id}/videos").json()
            job = next(j for j in jobs if j["id"] == job_id)
            if job["status"] in ("done", "failed", "cancelled"):
                return job
            time.sleep(0.3)
        pytest.fail("job did not finish in time")

    def test_job_lifecycle(self, client, project, tmp_path):
        video, _, _ = make_video(tmp_path / "clip.mp4", seconds_static=2, seconds_moving=2)
        with open(video, "rb") as fh:
            r = client.post(
                f"/api/projects/{project['id']}/videos/upload",
                files={"files": ("clip.mp4", fh, "video/mp4")},
                data={"params": "{}"},
            )
        assert r.status_code == 200, r.text
        job = r.json()[0]
        assert job["status"] in ("pending", "running")

        job = self._wait_job(client, project["id"], job["id"])
        assert job["status"] == "done", job["error"]
        assert job["progress"] == 1.0
        assert job["extracted_frames"] > 0
        assert job["fps"] > 0
        assert job["total_frames"] > 0
        assert job["decoded_frames"] == job["total_frames"]

        images = client.get(f"/api/projects/{project['id']}/images").json()
        assert len(images) == job["extracted_frames"]
        assert all(i["filename"].startswith("clip_f") for i in images)

    def test_upload_rejects_bad_params(self, client, project, tmp_path):
        video, _, _ = make_video(tmp_path / "clip.mp4", seconds_static=0, seconds_moving=1)
        with open(video, "rb") as fh:
            r = client.post(
                f"/api/projects/{project['id']}/videos/upload",
                files={"files": ("clip.mp4", fh, "video/mp4")},
                data={"params": '{"tiers": [[0.01, 5]]}'},
            )
        assert r.status_code == 400

    def test_upload_rejects_non_video(self, client, project):
        r = client.post(
            f"/api/projects/{project['id']}/videos/upload",
            files={"files": ("notes.txt", b"hello", "text/plain")},
        )
        assert r.status_code == 400

    def test_list_requires_membership(self, client, alice, bob, project):
        # Switch the session to bob, who is not a project member.
        client.post("/api/auth/login", json={"email": "bob@test.com", "password": "pass456"})
        r = client.get(f"/api/projects/{project['id']}/videos")
        assert r.status_code == 403

    def test_cancel_missing_job(self, client, project):
        r = client.post(f"/api/projects/{project['id']}/videos/9999/cancel")
        assert r.status_code == 404

    def test_upload_progress_endpoint(self, client, project, tmp_path):
        video, _, _ = make_video(tmp_path / "p.mp4", seconds_static=0, seconds_moving=1)
        with open(video, "rb") as fh:
            r = client.post(f"/api/projects/{project['id']}/videos/upload",
                            files={"files": ("p.mp4", fh, "video/mp4")},
                            data={"params": "{}", "upload_id": "test-upload-1"})
        assert r.status_code == 200, r.text
        p = client.get("/api/uploads/test-upload-1/progress").json()
        assert p["done"] is True
        assert p["file_count"] == 1
        assert p["filename"] == "p.mp4"
        assert p["saved"] > 0
        self._wait_job(client, project["id"], r.json()[0]["id"])

    def test_upload_progress_unknown_id(self, client, project):
        assert client.get("/api/uploads/nope/progress").status_code == 404

    def test_upload_progress_requires_login(self, client):
        assert client.get("/api/uploads/x/progress").status_code == 401

    def test_multiple_videos_run_to_completion(self, client, project, tmp_path):
        videos = []
        for name in ("a.mp4", "b.mp4"):
            v, _, _ = make_video(tmp_path / name, seconds_static=1, seconds_moving=1)
            videos.append((name, open(v, "rb"), "video/mp4"))
        r = client.post(f"/api/projects/{project['id']}/videos/upload",
                        files=[("files", v) for v in videos], data={"params": "{}"})
        assert r.status_code == 200, r.text
        jobs = r.json()
        assert len(jobs) == 2
        done_jobs = []
        for j in jobs:
            done = self._wait_job(client, project["id"], j["id"])
            assert done["status"] == "done", done["error"]
            done_jobs.append(done)
        images = client.get(f"/api/projects/{project['id']}/images").json()
        assert len(images) == sum(j["extracted_frames"] for j in done_jobs)

    def test_cancel_queued_job(self, client, project):
        # A job cancelled while still pending in the worker queue must end up
        # cancelled once the worker reaches it (not run).
        from sqlmodel import Session
        from app.db import engine
        from app.models import VideoJob
        from app.routers.videos import run_job
        with Session(engine) as s:
            job = VideoJob(project_id=project["id"], filename="v.mp4", stored_name="x.mp4",
                           cancel_requested=True, created_by=1)
            s.add(job)
            s.commit()
            s.refresh(job)
            job_id = job.id
        run_job(job_id)
        jobs = client.get(f"/api/projects/{project['id']}/videos").json()
        assert next(j for j in jobs if j["id"] == job_id)["status"] == "cancelled"
