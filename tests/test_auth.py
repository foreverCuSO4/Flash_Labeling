class TestRegister:
    def test_register(self, client):
        r = client.post("/api/auth/register", json={
            "email": "a@b.com", "name": "A", "password": "secret123",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["email"] == "a@b.com"
        assert body["name"] == "A"
        assert "id" in body

    def test_register_duplicate_email(self, client, alice):
        r = client.post("/api/auth/register", json={
            "email": "alice@test.com", "name": "Alice2", "password": "other",
        })
        assert r.status_code == 409

    def test_register_sets_cookie(self, client):
        r = client.post("/api/auth/register", json={
            "email": "c@d.com", "name": "C", "password": "secret123",
        })
        assert "yololabel_session" in r.cookies


class TestLogin:
    def test_login(self, client, alice):
        r = client.post("/api/auth/login", json={
            "email": "alice@test.com", "password": "pass123",
        })
        assert r.status_code == 200
        assert r.json()["email"] == "alice@test.com"

    def test_login_wrong_password(self, client, alice):
        r = client.post("/api/auth/login", json={
            "email": "alice@test.com", "password": "wrong",
        })
        assert r.status_code == 401

    def test_login_nonexistent(self, client):
        r = client.post("/api/auth/login", json={
            "email": "no@one.com", "password": "x",
        })
        assert r.status_code == 401


class TestMe:
    def test_me_authenticated(self, client, alice):
        r = client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["email"] == "alice@test.com"

    def test_me_unauthenticated(self, client):
        client.cookies.clear()
        r = client.get("/api/auth/me")
        assert r.status_code == 401


class TestLogout:
    def test_logout(self, client, alice):
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        client.cookies.clear()
        r = client.get("/api/auth/me")
        assert r.status_code == 401
