"""Video import: fixed-interval extraction logic + API job lifecycle."""
import time

import cv2
import numpy as np
import pytest

from app.video import (
    Cancelled,
    ExtractParams,
    ParamsError,
    extract_frames,
)


def make_video(path, fps=30, seconds_static=4, seconds_moving=4, size=(320, 240)):
    """Solid gray segment followed by a moving bright box."""
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
        assert p.interval == 0.2

    def test_from_dict(self):
        p = ExtractParams.from_dict({
            "interval": 1.5,
            "max_frames": 10,
            "jpeg_quality": 80,
        })
        assert p.interval == 1.5
        assert p.max_frames == 10
        assert p.jpeg_quality == 80

    def test_roundtrip(self):
        d = ExtractParams().to_dict()
        assert ExtractParams.from_dict(d).interval == ExtractParams().interval

    @pytest.mark.parametrize("bad", [
        {"interval": 0},
        {"interval": 3601},
        {"max_frames": 0},
        {"jpeg_quality": 200},
    ])
    def test_invalid(self, bad):
        with pytest.raises(ParamsError):
            ExtractParams.from_dict(bad)


class TestExtractFrames:
    def test_fixed_interval(self, tmp_path):
        video, n_static, n_moving = make_video(tmp_path / "clip.mp4")
        out = tmp_path / "frames"
        result = extract_frames(video, out, ExtractParams(interval=1.0))
        idxs = [f["frame_idx"] for f in result["frames"]]
        assert idxs[0] == 0  # first frame is always sampled
        assert all(25 <= (b - a) <= 35 for a, b in zip(idxs, idxs[1:]))
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


class TestParallelExtract:
    """workers>1 splits the video into ranges decoded concurrently; boundary
    frames may be sampled once extra per split, so counts differ slightly."""

    def test_workers_match_single_thread_closely(self, tmp_path):
        video, n_static, _ = make_video(tmp_path / "clip.mp4",
                                        seconds_static=20, seconds_moving=60)
        p = ExtractParams()
        r1 = extract_frames(video, tmp_path / "w1", p, workers=1)
        r2 = extract_frames(video, tmp_path / "w2", p, workers=2)
        assert r2["fps"] == r1["fps"]
        assert r2["total_frames"] == r1["total_frames"]
        assert abs(len(r2["frames"]) - len(r1["frames"])) <= 2
        idx2 = [f["frame_idx"] for f in r2["frames"]]
        assert idx2 == sorted(idx2) and 0 in idx2
        assert all(4 <= (b - a) <= 8 for a, b in zip(idx2, idx2[1:]))
        for f in r2["frames"]:
            img = cv2.imread(str(tmp_path / "w2" / f["stored_name"]))
            assert img is not None

    def test_parallel_max_frames(self, tmp_path):
        video, _, _ = make_video(tmp_path / "clip.mp4",
                                 seconds_static=0, seconds_moving=70)
        r = extract_frames(video, tmp_path / "out", ExtractParams(max_frames=3), workers=2)
        assert r["capped"] is True
        assert len(r["frames"]) == 3

    def test_parallel_cancel(self, tmp_path):
        video, _, _ = make_video(tmp_path / "clip.mp4",
                                 seconds_static=0, seconds_moving=70)
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 2

        with pytest.raises(Cancelled):
            extract_frames(video, tmp_path / "out", ExtractParams(),
                           workers=2, should_cancel=cancel)

    def test_short_video_falls_back_to_single_thread(self, tmp_path):
        # Below the per-worker frame floor, ranges collapse to one segment.
        video, _, _ = make_video(tmp_path / "clip.mp4", seconds_static=1, seconds_moving=1)
        r = extract_frames(video, tmp_path / "out", ExtractParams(), workers=8)
        assert len(r["frames"]) >= 1


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
                data={"params": '{"interval": 0}'},
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
