"""End-to-end tests for the upload → serve → list → delete cycle."""

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app


def _upload(client, html, slug="", filename="deck.html", token="test-owner"):
    headers = {"X-Owner-Token": token} if token else {}
    return client.post(
        "/api/presentations",
        files={"file": (filename, html.encode(), "text/html")},
        data={"slug": slug},
        headers=headers,
    )


def _page(title, body="hi"):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def _delete(client, slug, token="test-owner"):
    headers = {"X-Owner-Token": token} if token else {}
    return client.delete(f"/api/presentations/{slug}", headers=headers)


def test_full_cycle():
    with TestClient(app) as client:
        # upload with slug derived from <title>
        r = _upload(client, "<html><head><title>My Great Deck</title></head><body>hi</body></html>")
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["slug"] == "my-great-deck"
        assert body["title"] == "My Great Deck"
        assert body["replaced"] is False

        # served verbatim at /{slug}
        page = client.get("/my-great-deck")
        assert page.status_code == 200
        assert "My Great Deck" in page.text

        # derived collision gets a suffix, never clobbers
        r2 = _upload(
            client, "<html><head><title>My Great Deck</title></head><body>v2</body></html>"
        )
        assert r2.json()["slug"] == "my-great-deck-2"

        # explicit same slug replaces
        r3 = _upload(
            client,
            "<html><head><title>My Great Deck</title></head><body>v3</body></html>",
            slug="my-great-deck",
        )
        assert r3.json()["replaced"] is True
        assert "v3" in client.get("/my-great-deck").text

        # tokenless listing reveals nothing, including ownerless legacy pages
        assert client.get("/api/presentations").json() == []

        # delete
        assert _delete(client, "my-great-deck-2").status_code == 204
        assert client.get("/my-great-deck-2").status_code == 404


def test_rejections():
    with TestClient(app) as client:
        # non-html filename
        r = client.post(
            "/api/presentations",
            files={"file": ("notes.txt", b"hello", "text/plain")},
            data={"slug": ""},
        )
        assert r.status_code == 400

        # reserved slug
        r = _upload(client, "<html><head><title>x</title></head></html>", slug="docs")
        assert r.status_code == 400
        assert "reserved" in r.json()["detail"]

        # bad slug characters
        r = _upload(client, "<html><head><title>x</title></head></html>", slug="Bad Slug!")
        # slugify normalizes it rather than rejecting outright
        assert r.status_code == 201
        assert r.json()["slug"] == "bad-slug"

        # no title, no slug, junk filename that slugifies to nothing
        r = _upload(client, "<html><body>anon</body></html>", filename="----.html")
        assert r.status_code == 400

        # reserved path never serves a presentation
        assert client.get("/health").status_code == 404


def test_ownership():
    with TestClient(app) as client:
        # Alice uploads with her token
        r = _upload(client, _page("Owned Deck"), token="alice-token")
        assert r.status_code == 201
        slug = r.json()["slug"]

        # Bob cannot replace it (explicit slug), with or without a token
        r_bob = _upload(client, _page("Owned Deck", "bob"), slug=slug, token="bob-token")
        assert r_bob.status_code == 403
        assert _upload(client, _page("Owned Deck", "anon"), slug=slug, token="").status_code == 400

        # Bob cannot delete it either
        assert _delete(client, slug, token="bob-token").status_code == 403
        assert _delete(client, slug, token="").status_code == 403

        # Alice can update (upsert) her own page
        r = _upload(client, _page("Owned Deck", "v2"), slug=slug, token="alice-token")
        assert r.status_code == 201 and r.json()["replaced"] is True
        assert "v2" in client.get(f"/{slug}").text

        # Alice can enumerate her page, while Bob cannot enumerate it
        alice_rows = client.get(
            "/api/presentations", headers={"X-Owner-Token": "alice-token"}
        ).json()
        assert {p["slug"] for p in alice_rows} == {slug}
        assert alice_rows[0]["owned"] is True
        assert alice_rows[0]["mine"] is True
        bob_rows = client.get("/api/presentations", headers={"X-Owner-Token": "bob-token"}).json()
        assert slug not in {p["slug"] for p in bob_rows}

        # and Alice can delete it
        assert _delete(client, slug, token="alice-token").status_code == 204


def test_listings_are_scoped_and_blank_tokens_reveal_nothing():
    with TestClient(app) as client:
        alice = _upload(client, _page("Alice Listing"), token="alice-list-token")
        bob = _upload(client, _page("Bob Listing"), token="bob-list-token")
        assert alice.status_code == 201
        assert bob.status_code == 201
        alice_slug = alice.json()["slug"]
        bob_slug = bob.json()["slug"]

        alice_rows = client.get(
            "/api/presentations", headers={"X-Owner-Token": "alice-list-token"}
        ).json()
        bob_rows = client.get(
            "/api/presentations", headers={"X-Owner-Token": "bob-list-token"}
        ).json()
        assert {p["slug"] for p in alice_rows} == {alice_slug}
        assert {p["slug"] for p in bob_rows} == {bob_slug}
        assert all(p["owned"] and p["mine"] for p in alice_rows + bob_rows)

        # Missing, explicitly blank, and whitespace-only tokens cannot enumerate anything.
        assert client.get("/api/presentations").json() == []
        assert client.get("/api/presentations", headers={"X-Owner-Token": ""}).json() == []
        assert client.get("/api/presentations", headers={"X-Owner-Token": "   "}).json() == []

        # Slug URLs remain directly accessible without an owner token.
        assert client.get(f"/{alice_slug}").status_code == 200
        assert client.get(f"/{bob_slug}").status_code == 200


def test_legacy_ownerless_pages_are_locked():
    """Legacy data must not be claimable by any authenticated caller."""
    from app.db import Presentation

    with TestClient(app) as client:
        assert client.get("/api/library").status_code == 200
        with SessionLocal() as db:
            db.add(
                Presentation(
                    slug="legacy-unowned",
                    title="Legacy",
                    html="<title>Legacy</title>",
                    size_bytes=21,
                    owner_hash=None,
                )
            )
            db.commit()
        assert client.get("/legacy-unowned").status_code == 200
        assert (
            _upload(
                client, _page("Legacy", "claimed"), slug="legacy-unowned", token="carol"
            ).status_code
            == 403
        )
        assert _delete(client, "legacy-unowned", token="carol").status_code == 403
        assert _upload(client, _page("New"), token="").status_code == 400
        assert _delete(client, "legacy-unowned", token="").status_code == 403


def test_landing_and_health():
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
        home = client.get("/")
        assert home.status_code == 200
        assert "Showcase" in home.text
