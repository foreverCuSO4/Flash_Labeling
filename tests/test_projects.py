def _login_bob(client):
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": "bob@test.com", "password": "pass456"})


class TestCreateProject:
    def test_create(self, client, project):
        assert project["name"] == "Demo"
        assert project["role"] == "owner"
        assert len(project["classes"]) == 2
        assert project["classes"][0]["name"] == "car"
        assert project["classes"][1]["name"] == "person"

    def test_create_unauthenticated(self, client):
        client.cookies.clear()
        r = client.post("/api/projects", json={"name": "X", "classes": []})
        assert r.status_code == 401


class TestListProjects:
    def test_list_own(self, client, alice, project):
        r = client.get("/api/projects")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["name"] == "Demo"

    def test_list_shows_all_projects_to_any_user(self, client, alice, bob, project):
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "bob@test.com", "password": "pass456"})
        r = client.get("/api/projects")
        assert len(r.json()) == 1
        assert r.json()[0]["name"] == "Demo"
        assert r.json()[0]["role"] is None


class TestJoin:
    def test_join(self, client, alice, bob, project):
        _login_bob(client)
        r = client.post(f"/api/projects/{project['id']}/join")
        assert r.status_code == 200
        assert r.json()["role"] == "annotator"
        members = client.get(f"/api/projects/{project['id']}/members").json()
        assert any(m["email"] == "bob@test.com" for m in members)

    def test_join_twice_conflict(self, client, alice, bob, project):
        _login_bob(client)
        client.post(f"/api/projects/{project['id']}/join")
        r = client.post(f"/api/projects/{project['id']}/join")
        assert r.status_code == 409

    def test_join_nonexistent_project(self, client, alice):
        r = client.post("/api/projects/9999/join")
        assert r.status_code == 404

    def test_join_unauthenticated(self, client, project):
        client.cookies.clear()
        r = client.post(f"/api/projects/{project['id']}/join")
        assert r.status_code == 401

    def test_non_member_cannot_claim_but_can_after_join(self, client, alice, bob, project):
        import io
        from PIL import Image as PILImage
        buf = io.BytesIO()
        PILImage.new("RGB", (100, 100)).save(buf, "PNG")
        buf.seek(0)
        img = client.post(f"/api/projects/{project['id']}/images/upload",
                          files={"files": ("t.png", buf, "image/png")}).json()[0]

        _login_bob(client)
        r = client.post(f"/api/projects/{project['id']}/images/{img['id']}/claim")
        assert r.status_code == 403
        client.post(f"/api/projects/{project['id']}/join")
        r = client.post(f"/api/projects/{project['id']}/images/{img['id']}/claim")
        assert r.status_code == 200


class TestMembers:
    def test_add_member(self, client, alice, bob, project):
        r = client.post(f"/api/projects/{project['id']}/members", json={"email": "bob@test.com"})
        assert r.status_code == 200
        assert r.json()["role"] == "annotator"

    def test_add_member_nonexistent_user(self, client, alice, project):
        r = client.post(f"/api/projects/{project['id']}/members", json={"email": "no@one.com"})
        assert r.status_code == 404

    def test_add_member_duplicate(self, client, alice, bob, project):
        client.post(f"/api/projects/{project['id']}/members", json={"email": "bob@test.com"})
        r = client.post(f"/api/projects/{project['id']}/members", json={"email": "bob@test.com"})
        assert r.status_code == 409

    def test_non_owner_cannot_add_member(self, client, alice, bob, project):
        client.post(f"/api/projects/{project['id']}/members", json={"email": "bob@test.com"})
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "bob@test.com", "password": "pass456"})
        r = client.post(f"/api/projects/{project['id']}/members", json={"email": "x@y.com"})
        assert r.status_code == 403

    def test_non_member_can_view_as_guest(self, client, alice, bob, project):
        _login_bob(client)
        r = client.get(f"/api/projects/{project['id']}")
        assert r.status_code == 200
        assert r.json()["role"] is None

    def test_remove_member(self, client, alice, bob, project):
        client.post(f"/api/projects/{project['id']}/members", json={"email": "bob@test.com"})
        r = client.delete(f"/api/projects/{project['id']}/members/{bob['id']}")
        assert r.status_code == 200

    def test_cannot_remove_owner(self, client, alice, project):
        r = client.delete(f"/api/projects/{project['id']}/members/{alice['id']}")
        assert r.status_code == 400
