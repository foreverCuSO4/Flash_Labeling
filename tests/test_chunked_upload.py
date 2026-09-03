"""Chunked resumable video upload: state machine, idempotency, completion."""
import time

import pytest

from test_videos import make_video


def _init(client, filename="a.mp4", size=10):
    r = client.post("/api/uploads", json={"filename": filename, "size": size})
    assert r.status_code == 200, r.text
    return r.json()["upload_id"]


def _chunk(client, upload_id, offset, data):
    return client.put(
        f"/api/uploads/{upload_id}/chunk?offset={offset}",
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )


def _login(client, email, password):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200


class TestChunkStateMachine:
    def test_init_and_resume_info(self, client, alice):
        upload_id = _init(client, size=1234)
        st = client.get(f"/api/uploads/{upload_id}").json()
        assert st["received"] == 0 and st["size"] == 1234 and st["filename"] == "a.mp4"

    def test_sequential_appends(self, client, alice):
        upload_id = _init(client, size=10)
        r = _chunk(client, upload_id, 0, b"012345")
        assert r.status_code == 200 and r.json()["received"] == 6
        r = _chunk(client, upload_id, 6, b"6789")
        assert r.status_code == 200 and r.json()["received"] == 10

    def test_gap_rejected_with_409(self, client, alice):
        upload_id = _init(client, size=10)
        _chunk(client, upload_id, 0, b"012345")
        r = _chunk(client, upload_id, 8, b"89")
        assert r.status_code == 409
        assert r.json()["detail"]["received"] == 6
        # After re-aligning to the reported offset the upload continues.
        r = _chunk(client, upload_id, 6, b"6789")
        assert r.status_code == 200 and r.json()["received"] == 10

    def test_resend_is_idempotent(self, client, alice):
        upload_id = _init(client, size=10)
        _chunk(client, upload_id, 0, b"012345")
        r = _chunk(client, upload_id, 0, b"012345")  # duplicate retry
        assert r.status_code == 200 and r.json()["received"] == 6

    def test_chunk_past_size_rejected(self, client, alice):
        upload_id = _init(client, size=4)
        r = _chunk(client, upload_id, 0, b"012345")
        assert r.status_code == 400

    def test_complete_too_early_conflict(self, client, alice, project):
        upload_id = _init(client, size=10)
        _chunk(client, upload_id, 0, b"012345")
        r = client.post(f"/api/uploads/{upload_id}/complete",
                        json={"project_id": project["id"], "params": {}})
        assert r.status_code == 409

    def test_invalid_init(self, client, alice):
        assert client.post("/api/uploads", json={"filename": "a.txt", "size": 10}).status_code == 400
        assert client.post("/api/uploads", json={"filename": "a.mp4", "size": 0}).status_code == 400

    def test_upload_is_private(self, client, alice, bob):
        _login(client, "alice@test.com", "pass123")
        upload_id = _init(client, size=10)
        _login(client, "bob@test.com", "pass456")
        assert client.get(f"/api/uploads/{upload_id}").status_code == 404
        assert _chunk(client, upload_id, 0, b"012345").status_code == 404
        assert client.post(f"/api/uploads/{upload_id}/complete",
                           json={"project_id": 1, "params": {}}).status_code == 404

    def test_complete_requires_membership(self, client, alice, bob, project):
        # project belongs to alice; bob is not a member.
        upload_id = _init(client, size=4)
        _chunk(client, upload_id, 0, b"0123")
        _login(client, "bob@test.com", "pass456")
        r = client.post(f"/api/uploads/{upload_id}/complete",
                        json={"project_id": project["id"], "params": {}})
        assert r.status_code == 404  # bob can't even see alice's upload

    def test_unknown_upload_404(self, client, alice):
        assert client.get("/api/uploads/whatever").status_code == 404
        assert _chunk(client, "whatever", 0, b"x").status_code == 404


class TestCompleteFlow:
    def test_chunked_video_becomes_images(self, client, project, tmp_path):
        video, _, _ = make_video(tmp_path / "chunked.mp4", seconds_static=1, seconds_moving=2)
        data = open(video, "rb").read()
        upload_id = _init(client, filename="chunked.mp4", size=len(data))
        # Split into uneven chunks and resend one to prove idempotency.
        mid = len(data) // 3
        assert _chunk(client, upload_id, 0, data[:mid]).status_code == 200
        assert _chunk(client, upload_id, 0, data[:10]).status_code == 200  # duplicate start
        assert _chunk(client, upload_id, mid, data[mid:]).json()["received"] == len(data)

        r = client.post(f"/api/uploads/{upload_id}/complete",
                        json={"project_id": project["id"], "params": {}})
        assert r.status_code == 200, r.text
        job = r.json()
        assert job["filename"] == "chunked.mp4"

        deadline = time.time() + 60
        while time.time() < deadline:
            j = client.get(f"/api/projects/{project['id']}/videos").json()[0]
            if j["status"] in ("done", "failed"):
                break
            time.sleep(0.3)
        assert j["status"] == "done", j["error"]
        images = client.get(f"/api/projects/{project['id']}/images").json()
        assert len(images) == j["extracted_frames"] > 0
        # Second complete must fail — the upload is gone.
        assert client.post(f"/api/uploads/{upload_id}/complete",
                           json={"project_id": project["id"], "params": {}}).status_code == 404
