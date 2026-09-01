import io
import zipfile

import pytest
from PIL import Image as PILImage

KPS = ["nose", "left_shoulder", "right_shoulder"]


@pytest.fixture()
def pose_project(client, alice):
    r = client.post("/api/projects", json={
        "name": "PoseDemo", "classes": ["person"], "mode": "pose",
        "keypoints": KPS, "skeleton": [[0, 1], [0, 2]],
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
    return r.json()[0]


POSE_INSTANCE = {
    "class_id": 1, "x": 0.5, "y": 0.5, "w": 0.4, "h": 0.6,
    "keypoints": [
        {"x": 0.5, "y": 0.3, "v": 2},
        {"x": 0.4, "y": 0.45, "v": 2},
        {"x": 0.6, "y": 0.45, "v": 1},
    ],
}


class TestPoseProject:
    def test_create_pose_project(self, client, pose_project):
        assert pose_project["mode"] == "pose"
        assert pose_project["keypoints"] == KPS
        assert pose_project["skeleton"] == [[0, 1], [0, 2]]

    def test_pose_requires_keypoints(self, client, alice):
        r = client.post("/api/projects", json={"name": "Bad", "classes": ["x"], "mode": "pose"})
        assert r.status_code == 400

    def test_invalid_mode(self, client, alice):
        r = client.post("/api/projects", json={"name": "Bad", "mode": "polygon"})
        assert r.status_code == 400

    def test_skeleton_out_of_range(self, client, alice):
        r = client.post("/api/projects", json={
            "name": "Bad", "classes": ["x"], "mode": "pose",
            "keypoints": ["a", "b"], "skeleton": [[0, 5]],
        })
        assert r.status_code == 400


class TestPoseAnnotations:
    def test_save_and_get(self, client, pose_project):
        img = _upload(client, pose_project["id"])
        r = client.put(f"/api/images/{img['id']}/annotations", json=[POSE_INSTANCE])
        assert r.status_code == 200
        anns = client.get(f"/api/images/{img['id']}/annotations").json()
        assert len(anns) == 1
        assert anns[0]["keypoints"] == POSE_INSTANCE["keypoints"]

    def test_missing_keypoints_rejected(self, client, pose_project):
        img = _upload(client, pose_project["id"])
        r = client.put(f"/api/images/{img['id']}/annotations",
                       json=[{"class_id": 1, "x": 0.5, "y": 0.5, "w": 0.4, "h": 0.6}])
        assert r.status_code == 400

    def test_wrong_keypoint_count(self, client, pose_project):
        img = _upload(client, pose_project["id"])
        bad = dict(POSE_INSTANCE, keypoints=[{"x": 0.5, "y": 0.3, "v": 2}])
        r = client.put(f"/api/images/{img['id']}/annotations", json=[bad])
        assert r.status_code == 400

    def test_invalid_visibility(self, client, pose_project):
        img = _upload(client, pose_project["id"])
        bad = dict(POSE_INSTANCE, keypoints=[
            {"x": 0.5, "y": 0.3, "v": 5},
            {"x": 0.4, "y": 0.45, "v": 2},
            {"x": 0.6, "y": 0.45, "v": 1},
        ])
        r = client.put(f"/api/images/{img['id']}/annotations", json=[bad])
        assert r.status_code == 400

    def test_keypoints_rejected_in_detection(self, client, project):
        img = _upload(client, project["id"])
        r = client.put(f"/api/images/{img['id']}/annotations",
                       json=[dict(POSE_INSTANCE)])
        assert r.status_code == 400


class TestPoseConfigPatch:
    def test_update_keypoints_before_annotations(self, client, pose_project):
        r = client.patch(f"/api/projects/{pose_project['id']}",
                         json={"keypoints": KPS + ["hip"], "skeleton": [[0, 1], [0, 2], [1, 3]]})
        assert r.status_code == 200
        assert len(r.json()["keypoints"]) == 4

    def test_keypoint_count_change_blocked_after_annotations(self, client, pose_project):
        img = _upload(client, pose_project["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=[POSE_INSTANCE])
        r = client.patch(f"/api/projects/{pose_project['id']}", json={"keypoints": KPS + ["hip"]})
        assert r.status_code == 409

    def test_keypoints_rejected_on_detection_project(self, client, project):
        r = client.patch(f"/api/projects/{project['id']}", json={"keypoints": ["a"]})
        assert r.status_code == 400


class TestPoseExport:
    def test_export_pose_format(self, client, pose_project):
        img = _upload(client, pose_project["id"])
        client.put(f"/api/images/{img['id']}/annotations", json=[POSE_INSTANCE])
        r = client.get(f"/api/projects/{pose_project['id']}/export")
        assert r.status_code == 200
        zf = zipfile.ZipFile(io.BytesIO(r.content))

        assert "data.yaml" in zf.namelist()
        yaml_text = zf.read("data.yaml").decode()
        assert "kpt_shape: [3, 3]" in yaml_text
        assert '"person"' in yaml_text

        label = zf.read(f"labels/{img['id']}.txt").decode().strip()
        parts = label.split()
        # 1 class + 4 box + 3 keypoints * 3 values = 14
        assert len(parts) == 14
        assert parts[0] == "0"
        assert abs(float(parts[1]) - 0.5) < 1e-5
        # first keypoint
        assert abs(float(parts[5]) - 0.5) < 1e-5
        assert abs(float(parts[6]) - 0.3) < 1e-5
        assert parts[7] == "2"
        # third keypoint occluded
        assert parts[13] == "1"
