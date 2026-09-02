"""Create-project-from-dataset.yaml upload endpoint."""
import pytest


def _upload(client, content: bytes, filename="dataset.yaml", **form):
    return client.post(
        "/api/projects/from-yaml",
        files={"file": (filename, content, "application/x-yaml")},
        data=form,
    )


class TestFromYaml:
    def test_detection_names_list(self, client, alice):
        r = _upload(client, b"""
path: .
train: images/train
val: images/val
names:
  - cat
  - dog
""")
        assert r.status_code == 200, r.text
        proj = r.json()
        assert proj["mode"] == "detection"
        assert [c["name"] for c in proj["classes"]] == ["cat", "dog"]
        assert proj["name"] == "dataset"  # falls back to the uploaded filename stem

    def test_quadrilateral_dict_names(self, client, alice):
        # Mirrors the Armor2024Dataset yaml: kpt_shape present means the 4
        # points are keypoints for pose training, which beats the
        # polygon-looking annotation_format.
        r = _upload(client, b"""
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
        assert r.status_code == 200, r.text
        proj = r.json()
        assert proj["mode"] == "pose"
        assert proj["keypoints"] == [f"kp_{i}" for i in range(4)]
        assert proj["name"] == "Armor2024Dataset"
        assert [c["name"] for c in proj["classes"]] == ["B-G", "B-1", "R-G"]

    def test_segment_by_annotation_format(self, client, alice):
        # Polygon-style annotation_format without kpt_shape -> segment mode.
        r = _upload(client, b"task: segmentation\nannotation_format: class_id x1 y1 x2 y2 x3 y3\nnames: [plate]\n")
        assert r.status_code == 200
        assert r.json()["mode"] == "segment"

    def test_pose_kpt_shape(self, client, alice):
        r = _upload(client, b"names: [person]\nkpt_shape: [5, 3]\n")
        assert r.status_code == 200
        proj = r.json()
        assert proj["mode"] == "pose"
        assert proj["keypoints"] == [f"kp_{i}" for i in range(5)]

    def test_pose_without_kpt_shape_fails(self, client, alice):
        r = _upload(client, b"task: pose\nnames: [person]\n")
        assert r.status_code == 400

    def test_name_override(self, client, alice):
        r = _upload(client, b"dataset_name: X\nnames: [cat]\n", name="Custom")
        assert r.json()["name"] == "Custom"

    def test_mode_override(self, client, alice):
        r = _upload(client, b"task: pose\nkpt_shape: [2, 3]\nnames: [cat]\n", mode="detection")
        assert r.json()["mode"] == "detection"

    def test_rejects_bad_extension(self, client, alice):
        r = _upload(client, b"names: [cat]\n", filename="notes.txt")
        assert r.status_code == 400

    def test_rejects_invalid_yaml(self, client, alice):
        r = _upload(client, b"names: [unclosed\n")
        assert r.status_code == 400

    def test_rejects_missing_names(self, client, alice):
        r = _upload(client, b"path: .\ntrain: images\n")
        assert r.status_code == 400

    def test_rejects_empty_names(self, client, alice):
        r = _upload(client, b"names:\n  0: ''\n")
        assert r.status_code == 400

    def test_rejects_empty_file(self, client, alice):
        r = _upload(client, b"")
        assert r.status_code == 400

    def test_requires_login(self, client):
        r = _upload(client, b"names: [cat]\n")
        assert r.status_code == 401
