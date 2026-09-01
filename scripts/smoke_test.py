#!/usr/bin/env python3
"""End-to-end smoke test: register → create project → upload → annotate → export → validate zip.

Usage:
    python scripts/smoke_test.py [BASE_URL]

Expects a running server at BASE_URL (default http://127.0.0.1:8000).
Uses a temporary DATA_DIR-independent flow — all state lives on the server side.
"""
import io
import json
import sys
import urllib.request
import urllib.error
import zipfile
from http.cookiejar import CookieJar
from PIL import Image as PILImage

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

cj = CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def api(method, path, body=None, files=None):
    url = f"{BASE}{path}"
    if files:
        # multipart upload
        import mimetypes
        boundary = "----SmokeBoundary"
        parts = []
        for field_name, (filename, data, content_type) in files.items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            .encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        payload = b"".join(parts)
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    elif body is not None:
        payload = json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(url, method=method)
    try:
        resp = opener.open(req)
        ct = resp.headers.get("Content-Type", "")
        data = resp.read()
        if "json" in ct:
            return resp.status, json.loads(data)
        return resp.status, data
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data)
        except json.JSONDecodeError:
            return e.code, data


def make_png(w=640, h=480):
    buf = io.BytesIO()
    PILImage.new("RGB", (w, h), (80, 120, 160)).save(buf, "PNG")
    return buf.getvalue()


def main():
    print(f"Smoke test against {BASE}\n")

    # 1. Health
    status, body = api("GET", "/api/health")
    check("health endpoint", status == 200 and body.get("status") == "ok")

    # 2. Register two users
    status, alice = api("POST", "/api/auth/register",
                        {"email": "smoke_alice@test.com", "name": "Alice", "password": "smoke123"})
    check("register alice", status == 200, f"got {status}: {alice}")

    # 3. Create project
    status, proj = api("POST", "/api/projects",
                       {"name": "SmokeTest", "classes": ["car", "person", "dog"]})
    check("create project", status == 200 and proj["name"] == "SmokeTest")
    proj_id = proj["id"]
    class_ids = {c["name"]: c["id"] for c in proj["classes"]}

    # 4. Upload image
    png_data = make_png()
    status, imgs = api("POST", f"/api/projects/{proj_id}/images/upload",
                       files={"files": ("smoke.png", png_data, "image/png")})
    check("upload image", status == 200 and len(imgs) == 1)
    img_id = imgs[0]["id"]
    check("image dimensions", imgs[0]["width"] == 640 and imgs[0]["height"] == 480)

    # 5. Claim
    status, claimed = api("POST", f"/api/projects/{proj_id}/images/{img_id}/claim")
    check("claim image", status == 200 and claimed["claimed_by"] is not None)

    # 6. Save annotations
    boxes = [
        {"class_id": class_ids["car"], "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.3},
        {"class_id": class_ids["person"], "x": 0.3, "y": 0.7, "w": 0.15, "h": 0.2},
        {"class_id": class_ids["dog"], "x": 0.8, "y": 0.2, "w": 0.1, "h": 0.1},
    ]
    status, result = api("PUT", f"/api/images/{img_id}/annotations", boxes)
    check("save annotations", status == 200 and result["count"] == 3)

    # 7. Verify annotations
    status, anns = api("GET", f"/api/images/{img_id}/annotations")
    check("get annotations", status == 200 and len(anns) == 3)
    class_names = sorted(a["class_name"] for a in anns)
    check("annotation classes", class_names == ["car", "dog", "person"])

    # 8. Export YOLO zip
    status, zip_data = api("GET", f"/api/projects/{proj_id}/export")
    check("export zip", status == 200)
    zf = zipfile.ZipFile(io.BytesIO(zip_data))
    names = zf.namelist()
    check("zip has classes.txt", "classes.txt" in names)
    check("zip has images/", any(n.startswith("images/") for n in names))
    check("zip has labels/", any(n.startswith("labels/") for n in names))

    classes_txt = zf.read("classes.txt").decode().strip().split("\n")
    check("classes.txt content", classes_txt == ["car", "person", "dog"],
          f"got {classes_txt}")

    label_file = [n for n in names if n.startswith("labels/")][0]
    lines = zf.read(label_file).decode().strip().split("\n")
    check("label line count", len(lines) == 3, f"got {len(lines)}")

    for line in lines:
        parts = line.split()
        check(f"label format: {line[:30]}...",
              len(parts) == 5 and parts[0].isdigit(),
              f"parts={parts}")
        x, y, w, h = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
        check(f"coords normalized: {line[:30]}...",
              0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1)

    # 9. Verify DB matches export
    status, anns2 = api("GET", f"/api/images/{img_id}/annotations")
    export_coords = set()
    for line in lines:
        p = line.split()
        export_coords.add((int(p[0]), round(float(p[1]), 4), round(float(p[2]), 4),
                           round(float(p[3]), 4), round(float(p[4]), 4)))
    db_coords = set()
    for a in anns2:
        db_coords.add((a["ord"], round(a["x"], 4), round(a["y"], 4),
                       round(a["w"], 4), round(a["h"], 4)))
    check("export matches DB", export_coords == db_coords,
          f"export={export_coords} db={db_coords}")

    # 10. Project settings: guidelines + class description
    status, patched = api("PATCH", f"/api/projects/{proj_id}",
                          {"guidelines": "# Smoke rules\n- box tightly", })
    check("patch guidelines", status == 200 and patched["guidelines"].startswith("# Smoke"))
    car_id = class_ids["car"]
    status, cls = api("PATCH", f"/api/projects/{proj_id}/classes/{car_id}",
                      {"description": "four-wheeled vehicle"})
    check("class description", status == 200 and cls["description"] == "four-wheeled vehicle")

    # 11. Pose project flow
    status, pproj = api("POST", "/api/projects", {
        "name": "SmokePose", "classes": ["person"], "mode": "pose",
        "keypoints": ["nose", "l_shoulder", "r_shoulder"], "skeleton": [[0, 1], [0, 2]],
    })
    check("create pose project", status == 200 and pproj["mode"] == "pose")
    pproj_id = pproj["id"]
    pcls_id = pproj["classes"][0]["id"]

    status, pimgs = api("POST", f"/api/projects/{pproj_id}/images/upload",
                        files={"files": ("pose.png", make_png(), "image/png")})
    check("pose upload", status == 200)
    pimg_id = pimgs[0]["id"]

    pose_boxes = [{
        "class_id": pcls_id, "x": 0.5, "y": 0.5, "w": 0.4, "h": 0.6,
        "keypoints": [{"x": 0.5, "y": 0.3, "v": 2}, {"x": 0.4, "y": 0.45, "v": 2}, {"x": 0.6, "y": 0.45, "v": 1}],
    }]
    status, result = api("PUT", f"/api/images/{pimg_id}/annotations", pose_boxes)
    check("save pose annotation", status == 200 and result["count"] == 1)

    status, zip_data = api("GET", f"/api/projects/{pproj_id}/export")
    check("pose export zip", status == 200)
    zf = zipfile.ZipFile(io.BytesIO(zip_data))
    check("pose data.yaml", "data.yaml" in zf.namelist())
    yaml_text = zf.read("data.yaml").decode()
    check("kpt_shape in data.yaml", "kpt_shape: [3, 3]" in yaml_text)
    plabel = zf.read(f"labels/{pimg_id}.txt").decode().strip().split()
    check("pose label format", len(plabel) == 14 and plabel[0] == "0",
          f"got {len(plabel)} parts")
    check("pose visibility values", plabel[7] == "2" and plabel[10] == "2" and plabel[13] == "1")

    # 12. Logout
    status, _ = api("POST", "/api/auth/logout")
    check("logout", status == 200)

    print(f"\n{'=' * 40}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
    print("All smoke tests passed.")


if __name__ == "__main__":
    main()
