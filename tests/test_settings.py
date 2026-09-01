class TestProjectPatch:
    def test_update_name(self, client, alice, project):
        r = client.patch(f"/api/projects/{project['id']}", json={"name": "Renamed"})
        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"

    def test_update_guidelines_markdown(self, client, alice, project):
        md = "# Rules\n- box **cars** tightly\n- skip occluded"
        r = client.patch(f"/api/projects/{project['id']}", json={"guidelines": md})
        assert r.status_code == 200
        assert r.json()["guidelines"] == md
        # persisted on re-fetch
        r = client.get(f"/api/projects/{project['id']}")
        assert r.json()["guidelines"] == md

    def test_annotator_cannot_patch(self, client, alice, bob, project):
        client.post(f"/api/projects/{project['id']}/members", json={"email": "bob@test.com"})
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "bob@test.com", "password": "pass456"})
        r = client.patch(f"/api/projects/{project['id']}", json={"name": "Hacked"})
        assert r.status_code == 403


class TestClassManagement:
    def test_update_description(self, client, alice, project):
        cid = project["classes"][0]["id"]
        r = client.patch(f"/api/projects/{project['id']}/classes/{cid}",
                         json={"description": "four-wheeled vehicle"})
        assert r.status_code == 200
        assert r.json()["description"] == "four-wheeled vehicle"

    def test_rename_conflict(self, client, alice, project):
        cid = project["classes"][0]["id"]
        r = client.patch(f"/api/projects/{project['id']}/classes/{cid}", json={"name": "person"})
        assert r.status_code == 409

    def test_add_class_with_description(self, client, alice, project):
        r = client.post(f"/api/projects/{project['id']}/classes",
                        json={"name": "truck", "description": "large goods vehicle"})
        assert r.status_code == 200
        assert r.json()["description"] == "large goods vehicle"
        assert r.json()["ord"] == 2

    def test_delete_class(self, client, alice, project):
        cid = project["classes"][1]["id"]
        r = client.delete(f"/api/projects/{project['id']}/classes/{cid}")
        assert r.status_code == 200
        r = client.get(f"/api/projects/{project['id']}")
        names = [c["name"] for c in r.json()["classes"]]
        assert names == ["car"]
        # ord re-packed
        assert r.json()["classes"][0]["ord"] == 0

    def test_delete_referenced_class_rejected(self, client, alice, project):
        import io
        from PIL import Image as PILImage
        buf = io.BytesIO()
        PILImage.new("RGB", (100, 100)).save(buf, "PNG")
        buf.seek(0)
        r = client.post(f"/api/projects/{project['id']}/images/upload",
                        files={"files": ("t.png", buf, "image/png")})
        img_id = r.json()[0]["id"]
        cid = project["classes"][0]["id"]
        client.post(f"/api/projects/{project['id']}/images/{img_id}/claim")
        client.put(f"/api/images/{img_id}/annotations",
                   json=[{"class_id": cid, "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2}])
        r = client.delete(f"/api/projects/{project['id']}/classes/{cid}")
        assert r.status_code == 409

    def test_non_owner_cannot_edit_class(self, client, alice, bob, project):
        cid = project["classes"][0]["id"]
        client.post(f"/api/projects/{project['id']}/members", json={"email": "bob@test.com"})
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"email": "bob@test.com", "password": "pass456"})
        r = client.patch(f"/api/projects/{project['id']}/classes/{cid}", json={"name": "x"})
        assert r.status_code == 403
