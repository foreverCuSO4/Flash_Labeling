import io
import re
import zipfile
from datetime import datetime, timedelta, timezone

from PIL import Image as PILImage
from sqlmodel import Session

from app.db import engine
from app.models import Image


def _make_png(w=640, h=480, color=(100, 150, 200)):
    buf = io.BytesIO()
    PILImage.new("RGB", (w, h), color).save(buf, "PNG")
    buf.seek(0)
    return buf


def _upload(client, project_id, filename="test.png"):
    buf = _make_png()
    r = client.post(
        f"/api/projects/{project_id}/images/upload",
        files={"files": (filename, buf, "image/png")},
    )
    assert r.status_code == 200
    return r.json()[0]


def _claim(client, project_id, image_id):
    r = client.post(f"/api/projects/{project_id}/images/{image_id}/claim")
    assert r.status_code == 200


def _login(client, email, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200


def _add_bob(client, project):
    client.post(f"/api/projects/{project['id']}/members", json={"email": "bob@test.com"})
    _login(client, "bob@test.com", "pass456")


def _age_claim(image_id, hours):
    with Session(engine) as s:
        img = s.get(Image, image_id)
        img.claimed_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        s.add(img)
        s.commit()


BOXES = [
    {"class_id": 1, "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.3},
    {"class_id": 2, "x": 0.3, "y": 0.7, "w": 0.1, "h": 0.1},
]


class TestUpload:
    def test_upload(self, client, project):
        img = _upload(client, project["id"])
        assert img["width"] == 640
        assert img["height"] == 480
        assert img["status"] == "unlabeled"

    def test_upload_invalid_file(self, client, project):
        r = client.post(
            f"/api/projects/{project['id']}/images/upload",
            files={"files": ("bad.txt", io.BytesIO(b"not an image"), "text/plain")},
        )
        assert r.status_code == 400


class TestClaim:
    def test_claim(self, client, project):
        img = _upload(client, project["id"])
        r = client.post(f"/api/projects/{project['id']}/images/{img['id']}/claim")
        assert r.status_code == 200
        assert r.json()["claimed_by"] is not None

    def test_double_claim_conflict(self, client, alice, bob, project):
        img = _upload(client, project["id"])
        client.post(f"/api/projects/{project['id']}/members", json={"email": "bob@test.com"})
        client.post(f"/api/projects/{project['id']}/images/{img['id']}/claim")

        _login(client, "bob@test.com", "pass456")
        r = client.post(f"/api/projects/{project['id']}/images/{img['id']}/claim")
        assert r.status_code == 409

    def test_release(self, client, project):
        img = _upload(client, project["id"])
        client.post(f"/api/projects/{project['id']}/images/{img['id']}/claim")
        r = client.post(f"/api/projects/{project['id']}/images/{img['id']}/release")
        assert r.status_code == 200
        assert r.json()["claimed_by"] is None

    def test_claim_expires_after_24h(self, client, alice, bob, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        _age_claim(img["id"], 25)

        r = client.get(f"/api/projects/{project['id']}/images")
        assert r.json()[0]["claim_expired"] is True

        _add_bob(client, project)
        r = client.post(f"/api/projects/{project['id']}/images/{img['id']}/claim")
        assert r.status_code == 200

    def test_claim_fresh_within_24h(self, client, alice, bob, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        _age_claim(img["id"], 23)

        _add_bob(client, project)
        r = client.post(f"/api/projects/{project['id']}/images/{img['id']}/claim")
        assert r.status_code == 409


class TestBatchClaim:
    def test_batch_claim(self, client, project):
        for _ in range(5):
            _upload(client, project["id"])
        r = client.post(f"/api/projects/{project['id']}/images/claim", json={"count": 3})
        assert r.status_code == 200
        assert r.json()["count"] == 3

        r = client.get(f"/api/projects/{project['id']}/images")
        assert sum(1 for i in r.json() if i["claimed_by"] is not None) == 3

    def test_batch_claim_skips_active_claims(self, client, project):
        for _ in range(5):
            _upload(client, project["id"])
        client.post(f"/api/projects/{project['id']}/images/claim", json={"count": 3})
        # My own active claims are skipped too: asking for 3 more gets the 2 left.
        r = client.post(f"/api/projects/{project['id']}/images/claim", json={"count": 3})
        assert r.json()["count"] == 2

    def test_batch_claim_skips_others(self, client, alice, bob, project):
        imgs = [_upload(client, project["id"]) for _ in range(4)]
        _claim(client, project["id"], imgs[0]["id"])
        _claim(client, project["id"], imgs[1]["id"])

        _add_bob(client, project)
        r = client.post(f"/api/projects/{project['id']}/images/claim", json={"count": 10})
        assert r.json()["count"] == 2
        claimed_ids = {i["id"] for i in r.json()["claimed"]}
        assert claimed_ids == {imgs[2]["id"], imgs[3]["id"]}

    def test_batch_claim_count_bounds(self, client, project):
        _upload(client, project["id"])
        assert client.post(f"/api/projects/{project['id']}/images/claim", json={"count": 0}).status_code == 422
        assert client.post(f"/api/projects/{project['id']}/images/claim", json={"count": -1}).status_code == 422
        assert client.post(f"/api/projects/{project['id']}/images/claim", json={"count": 501}).status_code == 422


class TestAnnotations:
    def test_save_and_get(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        r = client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        assert r.status_code == 200
        assert r.json()["count"] == 2

        r = client.get(f"/api/images/{img['id']}/annotations")
        anns = r.json()
        assert len(anns) == 2
        assert anns[0]["class_name"] == "car"
        assert anns[1]["class_name"] == "person"

    def test_save_requires_claim(self, client, project):
        img = _upload(client, project["id"])
        r = client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        assert r.status_code == 403

    def test_save_forbidden_for_other_claimer(self, client, alice, bob, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        _add_bob(client, project)
        r = client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        assert r.status_code == 403

    def test_save_after_claim_expired(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        _age_claim(img["id"], 25)
        r = client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        assert r.status_code == 403

    def test_save_updates_status(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        r = client.get(f"/api/projects/{project['id']}/images")
        assert r.json()[0]["status"] == "labeled"

    def test_save_empty_resets_status(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        # The claim survives labeling, so re-saving within the lease works.
        client.put(f"/api/images/{img['id']}/annotations", json=[])
        r = client.get(f"/api/projects/{project['id']}/images")
        assert r.json()[0]["status"] == "unlabeled"

    def test_save_invalid_class(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        r = client.put(f"/api/images/{img['id']}/annotations",
                       json=[{"class_id": 999, "x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1}])
        assert r.status_code == 400

    def test_save_out_of_bounds(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        r = client.put(f"/api/images/{img['id']}/annotations",
                       json=[{"class_id": 1, "x": 1.5, "y": 0.5, "w": 0.1, "h": 0.1}])
        assert r.status_code == 400

    def test_clear(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        r = client.delete(f"/api/images/{img['id']}/annotations")
        assert r.status_code == 200
        r = client.get(f"/api/images/{img['id']}/annotations")
        assert r.json() == []

    def test_clear_requires_claim(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        client.post(f"/api/projects/{project['id']}/images/{img['id']}/release")
        r = client.delete(f"/api/images/{img['id']}/annotations")
        assert r.status_code == 403


class TestStats:
    def test_stats_counts(self, client, alice, bob, project):
        imgs = [_upload(client, project["id"]) for _ in range(3)]
        # alice labels one, keeps one active claim, leaves one free
        _claim(client, project["id"], imgs[0]["id"])
        client.put(f"/api/images/{imgs[0]['id']}/annotations", json=BOXES)
        _claim(client, project["id"], imgs[1]["id"])

        _add_bob(client, project)
        _claim(client, project["id"], imgs[2]["id"])

        r = client.get(f"/api/projects/{project['id']}/stats")
        assert r.status_code == 200
        stats = {s["name"]: s for s in r.json()}
        assert stats["Alice"]["labeled_count"] == 1
        assert stats["Alice"]["claimed_count"] == 1
        assert stats["Bob"]["labeled_count"] == 0
        assert stats["Bob"]["claimed_count"] == 1

    def test_stats_exclude_expired_claims(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        _age_claim(img["id"], 25)
        r = client.get(f"/api/projects/{project['id']}/stats")
        assert r.json()[0]["claimed_count"] == 0

    def test_clear_resets_labeled_by(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        client.delete(f"/api/images/{img['id']}/annotations")
        r = client.get(f"/api/projects/{project['id']}/stats")
        assert r.json()[0]["labeled_count"] == 0


class TestCaching:
    def test_image_url_is_content_addressed(self, client, project):
        img = _upload(client, project["id"])
        assert re.fullmatch(rf"/api/images/{img['id']}/file\?v=[0-9a-f]{{32}}\.png", img["url"])
        r = client.get(img["url"])
        assert r.status_code == 200
        assert "immutable" in r.headers["cache-control"]

    def test_annotations_not_immutable_cached(self, client, project):
        img = _upload(client, project["id"])
        r = client.get(f"/api/images/{img['id']}/annotations")
        assert r.status_code == 200
        assert "immutable" not in r.headers.get("cache-control", "")


class TestExport:
    def test_export_zip_structure(self, client, project):
        img = _upload(client, project["id"])
        _claim(client, project["id"], img["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        r = client.get(f"/api/projects/{project['id']}/export")
        assert r.status_code == 200

        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        assert "classes.txt" in names
        assert any(n.startswith("images/") for n in names)
        assert any(n.startswith("labels/") for n in names)

        classes = zf.read("classes.txt").decode().strip().split("\n")
        assert classes == ["car", "person"]

        label_files = [n for n in names if n.startswith("labels/")]
        content = zf.read(label_files[0]).decode().strip().split("\n")
        assert len(content) == 2
        parts = content[0].split()
        assert parts[0] == "0"  # car ord
        assert abs(float(parts[1]) - 0.5) < 1e-5
        assert abs(float(parts[2]) - 0.5) < 1e-5
        assert abs(float(parts[3]) - 0.2) < 1e-5
        assert abs(float(parts[4]) - 0.3) < 1e-5

    def test_export_no_annotations(self, client, project):
        _upload(client, project["id"])
        r = client.get(f"/api/projects/{project['id']}/export")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        label_files = [n for n in zf.namelist() if n.startswith("labels/")]
        assert len(label_files) == 1
        assert zf.read(label_files[0]).decode().strip() == ""
