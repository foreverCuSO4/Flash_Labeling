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

    def test_list_empty_for_other_user(self, client, alice, bob, project):
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "bob@test.com", "password": "pass456"})
        r = client.get("/api/projects")
        assert r.json() == []


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

    def test_non_member_cannot_access(self, client, alice, bob, project):
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "bob@test.com", "password": "pass456"})
        r = client.get(f"/api/projects/{project['id']}")
        assert r.status_code == 403

    def test_remove_member(self, client, alice, bob, project):
        client.post(f"/api/projects/{project['id']}/members", json={"email": "bob@test.com"})
        r = client.delete(f"/api/projects/{project['id']}/members/{bob['id']}")
        assert r.status_code == 200

    def test_cannot_remove_owner(self, client, alice, project):
        r = client.delete(f"/api/projects/{project['id']}/members/{alice['id']}")
        assert r.status_code == 400
