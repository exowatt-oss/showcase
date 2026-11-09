"""Public reads and fail-closed writes across the API, including deck assets."""

from fastapi.testclient import TestClient
from forwarded_auth import ConfigurationError, get_settings

from app.main import app


def test_default_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("FORWARDED_AUTH_MODE", raising=False)
    monkeypatch.delenv("FORWARDED_AUTH_ALLOW_INSECURE_NONE_MODE", raising=False)
    monkeypatch.delenv("FORWARDED_AUTH_ISSUER", raising=False)
    try:
        get_settings().validate()
    except ConfigurationError:
        pass
    else:
        raise AssertionError("production startup accepted missing authentication configuration")


def test_no_insecure_none_mode_without_second_acknowledgment(monkeypatch):
    monkeypatch.setenv("FORWARDED_AUTH_MODE", "none")
    monkeypatch.delenv("FORWARDED_AUTH_ALLOW_INSECURE_NONE_MODE", raising=False)
    try:
        get_settings().validate()
    except ConfigurationError:
        pass
    else:
        raise AssertionError("insecure none mode was permitted without acknowledgement")


def test_missing_identity_rejects_all_writes_but_reads_remain_public(monkeypatch):
    # Test a configured OIDC mode with no bearer/proxy identity. No external
    # JWKS request is made when the caller supplies no credentials.
    monkeypatch.setenv("FORWARDED_AUTH_MODE", "oidc")
    monkeypatch.setenv("FORWARDED_AUTH_ISSUER", "https://id.example.org")
    monkeypatch.setenv("FORWARDED_AUTH_AUDIENCE", "showcase")
    monkeypatch.setenv("FORWARDED_AUTH_ALLOWED_EMAIL_DOMAINS", "example.org")
    monkeypatch.delenv("FORWARDED_AUTH_ALLOW_INSECURE_NONE_MODE", raising=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert (
            client.post(
                "/api/presentations",
                files={"file": ("x.html", b"<title>x</title>")},
                headers={"X-Owner-Token": "owner"},
            ).status_code
            == 401
        )
        assert client.post("/api/assets/missing", json={"hashes": []}).status_code == 401
        assert client.put("/api/assets/" + "a" * 64, content=b"hi").status_code == 401
        assert client.post("/api/decks/sample/versions", json={}).status_code == 401
        assert (
            client.delete(
                "/api/presentations/sample", headers={"X-Owner-Token": "owner"}
            ).status_code
            == 401
        )


def test_untrusted_html_uses_opaque_sandbox():
    with TestClient(app) as client:
        page = client.post(
            "/api/presentations",
            files={
                "file": ("evil.html", b"<title>Evil</title><script>localStorage.clear()</script>")
            },
            headers={"X-Owner-Token": "author"},
        )
        assert page.status_code == 201
        served = client.get(page.json()["path"])
        assert served.status_code == 200
        assert served.headers["content-security-policy"] == "sandbox allow-scripts"
        assert "allow-same-origin" not in served.headers["content-security-policy"]
        assert served.headers["x-content-type-options"] == "nosniff"
        assert client.get("/").headers["content-security-policy"] == "frame-ancestors 'none'"


def test_demo_mode_still_requires_owner_token():
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/presentations", files={"file": ("x.html", b"<title>x</title>")}
            ).status_code
            == 400
        )
        assert (
            client.post("/api/decks/sample/versions", json={"deck": {}, "files": {}}).status_code
            == 400
        )
