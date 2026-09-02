"""Create-project-from-dataset.yaml endpoint."""
import pytest


def _write_yaml(tmp_path, content: str) -> str:
    p = tmp_path / "dataset.yaml"
    p.write_text(content)
    return str(p)


class TestFromYaml:
    def test_detection_names_list(self, client, alice, tmp_path):
        path = _write_yaml(tmp_path, """
path: .
train: images/train
val: images/val
names:
  - cat
  - dog
""")
        r = client.post("/api/projects/from-yaml", json={"path": path})
        assert r.status_code == 200, r.text
        proj = r.json()
        assert proj["mode"] == "detection"
        assert [c["name"] for c in proj["classes"]] == ["cat", "dog"]
        assert proj["name"] == tmp_path.name  # inferred from the yaml's directory

    def test_quadrilateral_dict_names(self, client, alice, tmp_path):
        # Mirrors /home/zhujiayi/data/datasets/dataset.yaml (Armor2024Dataset):
        # kpt_shape present means the 4 points are keypoints for pose training,
        # which wins over the polygon-looking annotation_format.
        path = _write_yaml(tmp_path, """
dataset_name: Armor2024Dataset
task: quadrilateral_detection
annotation_format: class_id x1 y1 x2 y2 x3 y3 x4 y4
coordinates: normalized
kpt_shape: [4, 2]
names:
  0: B-G
  1: B-1
  2: R-G
""")
        r = client.post("/api/projects/from-yaml", json={"path": path})
        assert r.status_code == 200, r.text
        proj = r.json()
        assert proj["mode"] == "pose"
        assert proj["keypoints"] == [f"kp_{i}" for i in range(4)]
        assert proj["name"] == "Armor2024Dataset"
        assert [c["name"] for c in proj["classes"]] == ["B-G", "B-1", "R-G"]

    def test_segment_by_annotation_format(self, client, alice, tmp_path):
        # Polygon-style annotation_format without kpt_shape -> segment mode.
        path = _write_yaml(tmp_path, """
task: segmentation
annotation_format: class_id x1 y1 x2 y2 x3 y3 x4 y4
names: [plate]
""")
        r = client.post("/api/projects/from-yaml", json={"path": path})
        assert r.status_code == 200
        assert r.json()["mode"] == "segment"

    def test_pose_kpt_shape(self, client, alice, tmp_path):
        path = _write_yaml(tmp_path, "names: [person]\nkpt_shape: [5, 3]\n")
        r = client.post("/api/projects/from-yaml", json={"path": path})
        assert r.status_code == 200
        proj = r.json()
        assert proj["mode"] == "pose"
        assert proj["keypoints"] == [f"kp_{i}" for i in range(5)]

    def test_pose_without_kpt_shape_fails(self, client, alice, tmp_path):
        path = _write_yaml(tmp_path, "task: pose\nnames: [person]\n")
        r = client.post("/api/projects/from-yaml", json={"path": path})
        assert r.status_code == 400

    def test_name_override(self, client, alice, tmp_path):
        path = _write_yaml(tmp_path, "dataset_name: X\nnames: [cat]\n")
        r = client.post("/api/projects/from-yaml", json={"path": path, "name": "Custom"})
        assert r.json()["name"] == "Custom"

    def test_mode_override(self, client, alice, tmp_path):
        path = _write_yaml(tmp_path, "task: pose\nkpt_shape: [2, 3]\nnames: [cat]\n")
        r = client.post("/api/projects/from-yaml", json={"path": path, "mode": "detection"})
        assert r.json()["mode"] == "detection"

    def test_missing_file(self, client, alice):
        r = client.post("/api/projects/from-yaml", json={"path": "/nope/nothing.yaml"})
        assert r.status_code == 400

    def test_not_yaml_extension(self, client, alice, tmp_path):
        p = tmp_path / "data.txt"
        p.write_text("names: [cat]\n")
        r = client.post("/api/projects/from-yaml", json={"path": str(p)})
        assert r.status_code == 400

    def test_no_names(self, client, alice, tmp_path):
        path = _write_yaml(tmp_path, "path: .\ntrain: images\n")
        r = client.post("/api/projects/from-yaml", json={"path": path})
        assert r.status_code == 400

    def test_invalid_yaml(self, client, alice, tmp_path):
        p = tmp_path / "dataset.yaml"
        p.write_text("names: [unclosed\n")
        r = client.post("/api/projects/from-yaml", json={"path": str(p)})
        assert r.status_code == 400

    def test_requires_login(self, client, tmp_path):
        path = _write_yaml(tmp_path, "names: [cat]\n")
        r = client.post("/api/projects/from-yaml", json={"path": path})
        assert r.status_code == 401
