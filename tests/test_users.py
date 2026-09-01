import io

from PIL import Image as PILImage

from app.config import AVATAR_DIR


def _make_png(color=(100, 150, 200)):
    buf = io.BytesIO()
    PILImage.new("RGB", (64, 64), color).save(buf, "PNG")
    buf.seek(0)
    return buf


class TestListUsers:
    def test_list_users(self, client, alice, bob):
        r = client.get("/api/users")
        assert r.status_code == 200
        users = r.json()
        assert [u["email"] for u in users] == ["alice@test.com", "bob@test.com"]
        for u in users:
            assert u["avatar_url"] == f"/api/users/{u['id']}/avatar"
            assert "password_hash" not in u

    def test_list_users_unauthenticated(self, client):
        assert client.get("/api/users").status_code == 401


class TestAvatar:
    def test_default_avatar_is_svg(self, client, alice):
        r = client.get(f"/api/users/{alice['id']}/avatar")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")
        assert "<svg" in r.text

    def test_avatar_unknown_user(self, client, alice):
        assert client.get("/api/users/9999/avatar").status_code == 404

    def test_upload_avatar(self, client, alice):
        r = client.post("/api/users/me/avatar",
                        files={"file": ("a.png", _make_png(), "image/png")})
        assert r.status_code == 200
        assert r.json()["id"] == alice["id"]
        r = client.get(f"/api/users/{alice['id']}/avatar")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"

    def test_upload_avatar_replaces_old_file(self, client, alice):
        before = set(AVATAR_DIR.glob("*"))
        client.post("/api/users/me/avatar",
                    files={"file": ("a.png", _make_png(), "image/png")})
        client.post("/api/users/me/avatar",
                    files={"file": ("b.png", _make_png((1, 2, 3)), "image/png")})
        after = set(AVATAR_DIR.glob("*"))
        # Old avatar file was cleaned up — exactly one new file remains.
        assert len(after - before) == 1
        r = client.get(f"/api/users/{alice['id']}/avatar")
        assert r.headers["content-type"] == "image/png"

    def test_upload_avatar_invalid(self, client, alice):
        r = client.post("/api/users/me/avatar",
                        files={"file": ("bad.txt", io.BytesIO(b"nope"), "text/plain")})
        assert r.status_code == 400
        # Falls back to the default avatar afterwards.
        r = client.get(f"/api/users/{alice['id']}/avatar")
        assert r.headers["content-type"].startswith("image/svg+xml")

    def test_upload_avatar_unauthenticated(self, client):
        r = client.post("/api/users/me/avatar",
                        files={"file": ("a.png", _make_png(), "image/png")})
        assert r.status_code == 401
