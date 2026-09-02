"""Segment mode: polygon annotations, validation, YOLO segment export."""
import io
import zipfile

import pytest
from PIL import Image as PILImage


@pytest.fixture()
def seg_project(client, alice):
    r = client.post("/api/projects", json={
        "name": "SegDemo", "classes": ["plate", "car"], "mode": "segment",
    })
    assert r.status_code == 200
    return r.json()


def _upload(client, project_id):
    buf = io.BytesIO()
    PILImage.new("RGB", (640, 480)).save(buf, "PNG")
    buf.seek(0)
    r = client.post(f"/api/projects/{project_id}/images/upload",
                    files={"files": ("t.png", buf, "image/png")})
    assert r.status_code == 200
    img = r.json()[0]
    # Annotations require an active claim.
    r = client.post(f"/api/projects/{project_id}/images/{img['id']}/claim")
    assert r.status_code == 200
    return img


QUAD = {
    # bbox fields are ignored in segment mode — the server derives them from the polygon
    "class_id": 1, "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2,
    "polygon": [[0.2, 0.2], [0.6, 0.3], [0.55, 0.7], [0.15, 0.6]],
}


class TestSegmentAnnotations:
    def test_save_and_get(self, client, seg_project):
        img = _upload(client, seg_project["id"])
        r = client.put(f"/api/images/{img['id']}/annotations", json=[QUAD])
        assert r.status_code == 200, r.text
        anns = client.get(f"/api/images/{img['id']}/annotations").json()
        assert anns[0]["polygon"] == QUAD["polygon"]
        assert anns[0]["keypoints"] is None
        # bbox derived from the polygon: xs 0.15..0.6, ys 0.2..0.7
        assert anns[0]["x"] == pytest.approx(0.375)
        assert anns[0]["y"] == pytest.approx(0.45)
        assert anns[0]["w"] == pytest.approx(0.45)
        assert anns[0]["h"] == pytest.approx(0.5)

    def test_export_format(self, client, seg_project):
        img = _upload(client, seg_project["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=[QUAD])
        r = client.get(f"/api/projects/{seg_project['id']}/export")
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            label = zf.read(f"labels/{img['id']}.txt").decode().strip()
            data_yaml = zf.read("data.yaml").decode()
        parts = label.split()
        assert parts[0] == "0"  # class ord, not id
        assert len(parts) == 1 + 8  # class + 4 polygon points, no bbox
        assert parts[1:3] == ["0.200000", "0.200000"]
        assert "kpt_shape" not in data_yaml

    def test_polygon_required(self, client, seg_project):
        img = _upload(client, seg_project["id"])
        bad = {"class_id": 1, "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2}
        r = client.put(f"/api/images/{img['id']}/annotations", json=[bad])
        assert r.status_code == 400

    def test_polygon_needs_3_points(self, client, seg_project):
        img = _upload(client, seg_project["id"])
        bad = {**QUAD, "polygon": [[0.1, 0.1], [0.2, 0.2]]}
        r = client.put(f"/api/images/{img['id']}/annotations", json=[bad])
        assert r.status_code == 400

    def test_polygon_points_normalized(self, client, seg_project):
        img = _upload(client, seg_project["id"])
        bad = {**QUAD, "polygon": [[0.1, 0.1], [1.5, 0.2], [0.2, 0.4]]}
        r = client.put(f"/api/images/{img['id']}/annotations", json=[bad])
        assert r.status_code == 400

    def test_keypoints_rejected(self, client, seg_project):
        img = _upload(client, seg_project["id"])
        bad = {**QUAD, "keypoints": [{"x": 0.3, "y": 0.3, "v": 2}]}
        r = client.put(f"/api/images/{img['id']}/annotations", json=[bad])
        assert r.status_code == 400

    def test_polygon_rejected_in_detection(self, client, project):  # project fixture = detection mode
        img = _upload(client, project["id"])
        r = client.put(f"/api/images/{img['id']}/annotations", json=[QUAD])
        assert r.status_code == 400
