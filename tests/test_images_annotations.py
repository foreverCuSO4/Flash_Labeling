import io
import zipfile

from PIL import Image as PILImage


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

        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "bob@test.com", "password": "pass456"})
        r = client.post(f"/api/projects/{project['id']}/images/{img['id']}/claim")
        assert r.status_code == 409

    def test_release(self, client, project):
        img = _upload(client, project["id"])
        client.post(f"/api/projects/{project['id']}/images/{img['id']}/claim")
        r = client.post(f"/api/projects/{project['id']}/images/{img['id']}/release")
        assert r.status_code == 200
        assert r.json()["claimed_by"] is None


class TestAnnotations:
    def test_save_and_get(self, client, project):
        img = _upload(client, project["id"])
        r = client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        assert r.status_code == 200
        assert r.json()["count"] == 2

        r = client.get(f"/api/images/{img['id']}/annotations")
        anns = r.json()
        assert len(anns) == 2
        assert anns[0]["class_name"] == "car"
        assert anns[1]["class_name"] == "person"

    def test_save_updates_status(self, client, project):
        img = _upload(client, project["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        r = client.get(f"/api/projects/{project['id']}/images")
        assert r.json()[0]["status"] == "labeled"

    def test_save_empty_resets_status(self, client, project):
        img = _upload(client, project["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        client.put(f"/api/images/{img['id']}/annotations", json=[])
        r = client.get(f"/api/projects/{project['id']}/images")
        assert r.json()[0]["status"] == "unlabeled"

    def test_save_invalid_class(self, client, project):
        img = _upload(client, project["id"])
        r = client.put(f"/api/images/{img['id']}/annotations",
                       json=[{"class_id": 999, "x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1}])
        assert r.status_code == 400

    def test_save_out_of_bounds(self, client, project):
        img = _upload(client, project["id"])
        r = client.put(f"/api/images/{img['id']}/annotations",
                       json=[{"class_id": 1, "x": 1.5, "y": 0.5, "w": 0.1, "h": 0.1}])
        assert r.status_code == 400

    def test_clear(self, client, project):
        img = _upload(client, project["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=BOXES)
        r = client.delete(f"/api/images/{img['id']}/annotations")
        assert r.status_code == 200
        r = client.get(f"/api/images/{img['id']}/annotations")
        assert r.json() == []


class TestExport:
    def test_export_zip_structure(self, client, project):
        img = _upload(client, project["id"])
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
