"""Deck bundles: assets, versions, the viewer wrapper, asset ranges, ownership."""

import hashlib
import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text

from app import decks
from app.db import Asset, DeckVersion, SessionLocal, _migrate
from app.main import app


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _headers(token: str = "") -> dict:
    return {"X-Owner-Token": token} if token else {}


def _put(client, data: bytes, mime: str = "application/octet-stream"):
    return client.put(f"/api/assets/{_sha(data)}", content=data, headers={"Content-Type": mime})


def _store(client, files: dict[str, bytes]) -> dict[str, str]:
    out = {}
    for path, data in files.items():
        ext = path.rsplit(".", 1)[-1]
        response = _put(client, data, decks.mime_for_ext(ext) or "application/octet-stream")
        assert response.status_code in (200, 201), response.text
        out[path] = _sha(data)
    return out


def _push(client, slug, deck, files, token="test-owner", message=""):
    return client.post(
        f"/api/decks/{slug}/versions",
        json={"deck": deck, "files": files, "message": message},
        headers=_headers(token),
    )


def _upload_html(client, html, slug="", token="test-owner"):
    return client.post(
        "/api/presentations",
        files={"file": ("page.html", html.encode(), "text/html")},
        data={"slug": slug},
        headers=_headers(token),
    )


def _bundle_bytes(tag: str) -> dict[str, bytes]:
    return {
        "assets/bg.jpg": f"jpeg-{tag}".encode() * 10,
        "assets/clip.mp4": bytes(range(256)) * 40 + tag.encode(),
        "theme/extra.css": (
            b".slide{background:url(assets/bg.jpg)}\n"
            b".x{content:'x-assets/bg.jpg'}\n"  # preceded by '-': left alone
            b"/* </style> must not break out */"
        ),
        "theme/extra.js": b"window.__extra = 'assets/clip.mp4';",
        "slides/lab.html": (
            b'<section class="slide" data-slide="lab"><img src="assets/bg.jpg">'
            b"<p>assets/unknown.png</p></section>\n<script>if (1 < 2) {}</script>"
        ),
        "deck.json": f"deck-{tag}".encode(),
    }


def _deck(title="Example <demo> & co", **overrides) -> dict:
    deck = {
        "schema": 1,
        "title": title,
        "theme": "neutral",
        "css": ["theme/extra.css"],
        "scripts": ["theme/extra.js"],
        "flags": {"openSource": True},
        "fine": "Example.",
        "slides": [
            {
                "id": "opener",
                "layout": "title",
                "kicker": "Demo",
                "title": "Build a useful demo",
                "media": "assets/bg.jpg",
            },
            {
                "id": "proof",
                "layout": "stats",
                "title": "Offline by default",
                "stats": [{"value": "0.631 ms", "label": "a full sky"}],
                "notes": "Speaker notes",
            },
            {"id": "lab", "layout": "html", "html": "slides/lab.html"},
            {"id": "oss", "layout": "statement", "title": "Open", "when": "openSource"},
            {
                "id": "clip",
                "layout": "media",
                "media": {"src": "assets/clip.mp4", "poster": "assets/bg.jpg", "loop": True},
                "backup": True,
            },
        ],
    }
    deck.update(overrides)
    return deck


def _deck_data(page_html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="deck-data">(.*?)</script>', page_html, re.S
    )
    assert match, page_html
    return json.loads(match.group(1))


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _new_deck(client, token="test-owner", tag=None):
    tag = tag or _uid()
    files = _store(client, _bundle_bytes(tag))
    slug = f"deck-{tag}"
    response = _push(client, slug, _deck(), files, token=token, message="first")
    assert response.status_code == 201, response.text
    return slug, files


# --- assets ------------------------------------------------------------------


def test_assets_missing_put_and_hash_checks(client, monkeypatch):
    a, b = f"a-{_uid()}".encode(), f"b-{_uid()}".encode()
    response = client.post("/api/assets/missing", json={"hashes": [_sha(a), _sha(b), _sha(a)]})
    assert response.status_code == 200
    assert response.json() == {"missing": [_sha(a), _sha(b)]}

    first = _put(client, a, "image/png")
    assert first.status_code == 201, first.text
    assert first.json() == {"sha256": _sha(a), "size_bytes": len(a), "created": True}
    again = _put(client, a)
    assert again.status_code == 200 and again.json()["created"] is False

    response = client.post("/api/assets/missing", json={"hashes": [_sha(a), _sha(b).upper()]})
    assert response.json() == {"missing": [_sha(b)]}

    with SessionLocal() as db:
        row = db.get(Asset, _sha(a))
        assert row.mime == "image/png" and bytes(row.data) == a and row.size_bytes == len(a)

    mismatch = client.put(f"/api/assets/{_sha(b)}", content=a)
    assert mismatch.status_code == 400 and "sha256" in mismatch.json()["detail"]
    assert client.put("/api/assets/not-a-hash", content=a).status_code == 400

    for body in ({"hashes": "x"}, {"nope": []}, {"hashes": ["zz"]}, [1]):
        bad = client.post("/api/assets/missing", json=body)
        assert bad.status_code == 400, body
    bad_json = client.post(
        "/api/assets/missing", content=b"{", headers={"Content-Type": "application/json"}
    )
    assert bad_json.status_code == 400

    monkeypatch.setattr(decks, "MAX_ASSET_BYTES", 16)
    big = b"x" * 17
    too_big = client.put(f"/api/assets/{_sha(big)}", content=big)
    assert too_big.status_code == 413 and "MB" in too_big.json()["detail"]


# --- push and serve -------------------------------------------------------------


def test_push_serve_latest_pinned_and_versions(client):
    token = f"owner-{_uid()}"
    tag = _uid()
    files = _store(client, _bundle_bytes(tag))
    slug = f"deck-{tag}"

    first = _push(client, slug, _deck(), files, token=token, message="first")
    assert first.status_code == 201, first.text
    total = sum(len(data) for data in _bundle_bytes(tag).values())
    assert first.json() == {
        "slug": slug,
        "version": 1,
        "url": f"/{slug}",
        "pinned_url": f"/{slug}/v1",
        "size_bytes": total,
    }

    page = client.get(f"/{slug}")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-cache"
    etag = page.headers["etag"]
    assert etag.startswith(f'"{slug}-v1-{decks.RUNTIME.hash}')
    body = page.text
    rt = f"/{slug}/_rt/{decks.RUNTIME.hash}"
    assert body.startswith('<!doctype html>\n<html lang="en">')
    assert '<meta charset="utf-8">' in body and 'name="viewport"' in body
    assert "<title>Example &lt;demo&gt; &amp; co</title>" in body
    assert f'<link rel="stylesheet" href="{rt}/stage.css">' in body
    assert f'<link rel="stylesheet" href="{rt}/theme-neutral.css">' in body
    assert '<div id="deck-root"></div>' in body
    assert f'<script src="{rt}/runtime.js" defer></script>' in body

    bg = f"/{slug}/_a/{files['assets/bg.jpg']}.jpg"
    clip = f"/{slug}/_a/{files['assets/clip.mp4']}.mp4"
    style = re.search(r'<style id="deck-css">(.*?)</style>', body, re.S).group(1)
    assert f"url({bg})" in style
    assert "x-assets/bg.jpg" in style  # preceded by '-': not a reference
    assert "<\\/style>" in style

    data = _deck_data(body)
    assert set(data) == {
        "slug",
        "version",
        "title",
        "theme",
        "flags",
        "fine",
        "slides",
        "scriptSources",
    }
    assert data["slug"] == slug and data["version"] == 1
    assert data["title"] == "Example <demo> & co"
    assert data["theme"] == "neutral" and data["flags"] == {"openSource": True}
    assert data["fine"] == "Example."
    slides = {slide["id"]: slide for slide in data["slides"]}
    assert [s["id"] for s in data["slides"]] == ["opener", "proof", "lab", "oss", "clip"]
    assert slides["opener"]["media"] == bg
    assert slides["proof"]["stats"] == [{"value": "0.631 ms", "label": "a full sky"}]
    assert slides["proof"]["notes"] == "Speaker notes"
    assert slides["clip"]["media"] == {"src": clip, "poster": bg, "loop": True}
    assert slides["clip"]["backup"] is True and slides["oss"]["when"] == "openSource"
    source = slides["lab"]["source"]
    assert f'<img src="{bg}">' in source
    assert "assets/unknown.png" in source  # not in the bundle: left alone
    assert "if (1 < 2)" in source
    assert data["scriptSources"] == [f"window.__extra = '{clip}';"]
    # Every "<" inside the JSON is escaped, so no </script> can end it early.
    raw_json = re.search(r'id="deck-data">(.*?)</script>', body, re.S).group(1)
    assert "<" not in raw_json and "\\u003c" in raw_json

    not_modified = client.get(f"/{slug}", headers={"If-None-Match": etag})
    assert not_modified.status_code == 304 and not_modified.content == b""
    assert not_modified.headers["etag"] == etag

    # v2: new title and one slide fewer
    deck2 = _deck(title="Second", slides=_deck()["slides"][:2], css=[], scripts=[])
    second = _push(client, slug, deck2, files, token=token, message="second")
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2 and second.json()["pinned_url"] == f"/{slug}/v2"

    latest = client.get(f"/{slug}", headers={"If-None-Match": etag})
    assert latest.status_code == 200 and latest.headers["etag"] != etag
    assert _deck_data(latest.text)["version"] == 2
    assert _deck_data(latest.text)["scriptSources"] == []
    assert "<title>Second</title>" in latest.text

    pinned = client.get(f"/{slug}/v1")
    assert pinned.status_code == 200
    assert pinned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert _deck_data(pinned.text)["version"] == 1
    assert _deck_data(pinned.text)["title"] == "Example <demo> & co"
    pinned_304 = client.get(f"/{slug}/v1", headers={"If-None-Match": pinned.headers["etag"]})
    assert pinned_304.status_code == 304
    assert client.get(f"/{slug}/v3").status_code == 404
    assert client.get(f"/{slug}/v0").status_code == 404

    versions = client.get(f"/api/decks/{slug}/versions")
    assert versions.status_code == 200
    listed = versions.json()
    assert [v["version"] for v in listed] == [2, 1]
    assert [v["message"] for v in listed] == ["second", "first"]
    assert listed[1]["size_bytes"] == total and listed[0]["created_at"]

    one = client.get(f"/api/decks/{slug}/versions/1").json()
    assert one["version"] == 1 and one["message"] == "first"
    assert one["deck"] == _deck() and one["files"] == files
    assert client.get(f"/api/decks/{slug}/versions/9").status_code == 404
    assert client.get(f"/api/decks/nope-{_uid()}/versions").status_code == 404


def test_theme_none_skips_the_theme_stylesheet(client):
    files = _store(client, {"assets/x.png": f"png-{_uid()}".encode()})
    slug = f"plain-{_uid()}"
    deck = {
        "schema": 1,
        "title": "Plain",
        "theme": "none",
        "slides": [{"id": "only", "layout": "media", "media": "assets/x.png"}],
    }
    assert _push(client, slug, deck, files).status_code == 201
    page = client.get(f"/{slug}").text
    assert "theme-neutral.css" not in page and "stage.css" in page
    assert '<style id="deck-css"></style>' in page
    data = _deck_data(page)
    assert data["theme"] == "none" and data["flags"] == {} and data["fine"] == ""


def test_wrapper_cache_is_used_and_keyed_by_runtime(client, monkeypatch):
    slug, _files = _new_deck(client)
    first = client.get(f"/{slug}").text
    calls = []
    original = decks.render_wrapper

    def counting(**kwargs):
        calls.append(kwargs["slug"])
        return original(**kwargs)

    monkeypatch.setattr(decks, "render_wrapper", counting)
    assert client.get(f"/{slug}").text == first
    assert calls == []
    monkeypatch.setattr(decks, "RUNTIME", decks.Runtime("abcdef012345", {}, {}))
    assert "/_rt/abcdef012345/" in client.get(f"/{slug}").text
    assert calls == [slug]


# --- assets: bytes, ranges -------------------------------------------------------


def test_asset_bytes_ranges_and_caching(client):
    slug, files = _new_deck(client)
    clip_sha = files["assets/clip.mp4"]
    with SessionLocal() as db:
        clip = bytes(db.get(Asset, clip_sha).data)
    url = f"/{slug}/_a/{clip_sha}.mp4"

    full = client.get(url)
    assert full.status_code == 200 and full.content == clip
    assert full.headers["content-type"] == "video/mp4"
    assert full.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert full.headers["etag"] == f'"{clip_sha}"'
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-length"] == str(len(clip))

    part = client.get(url, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == clip[:100]
    assert part.headers["content-range"] == f"bytes 0-99/{len(clip)}"
    assert part.headers["content-length"] == "100"

    # Safari's probe: the first two bytes.
    probe = client.get(url, headers={"Range": "bytes=0-1"})
    assert probe.status_code == 206 and probe.content == clip[:2]

    tail = client.get(url, headers={"Range": "bytes=1000-"})
    assert tail.status_code == 206 and tail.content == clip[1000:]
    assert tail.headers["content-range"] == f"bytes 1000-{len(clip) - 1}/{len(clip)}"

    suffix = client.get(url, headers={"Range": "bytes=-50"})
    assert suffix.status_code == 206 and suffix.content == clip[-50:]

    clamp = client.get(url, headers={"Range": f"bytes=10-{len(clip) * 2}"})
    assert clamp.status_code == 206 and clamp.content == clip[10:]

    for bad in (f"bytes={len(clip)}-", "bytes=50-10", "bytes=abc", "bytes=-0"):
        unsatisfiable = client.get(url, headers={"Range": bad})
        assert unsatisfiable.status_code == 416, bad
        assert unsatisfiable.headers["content-range"] == f"bytes */{len(clip)}"

    multi = client.get(url, headers={"Range": "bytes=0-1,5-6"})
    assert multi.status_code == 200 and multi.content == clip

    cached = client.get(url, headers={"If-None-Match": f'"{clip_sha}"'})
    assert cached.status_code == 304 and cached.content == b""

    head = client.head(url, headers={"Range": "bytes=0-9"})
    assert head.status_code == 206 and head.headers["content-length"] == "10"

    # The stored MIME decides the type; URL extensions cannot spoof it.
    data = f"svg-{_uid()}".encode()
    assert _put(client, data, "image/svg+xml").status_code == 201
    assert client.get(f"/{slug}/_a/{_sha(data)}.svg").status_code == 404
    assert _push(client, slug, _deck(), {**files, "assets/icon.svg": _sha(data)}).status_code == 201
    assert client.get(f"/{slug}/_a/{_sha(data)}.svg").headers["content-type"] == "image/svg+xml"
    fallback = client.get(f"/{slug}/_a/{_sha(data)}.html")
    assert fallback.headers["content-type"] == "image/svg+xml"
    assert fallback.headers["x-content-type-options"] == "nosniff"

    assert client.get(f"/{slug}/_a/{'0' * 64}.mp4").status_code == 404
    assert client.get(f"/{slug}/_a/not-a-hash.mp4").status_code == 404
    assert client.get(f"/nope-{_uid()}/_a/{clip_sha}.mp4").status_code == 404
    html_slug = _upload_html(client, f"<title>Page {_uid()}</title>").json()["slug"]
    assert client.get(f"/{html_slug}/_a/{clip_sha}.mp4").status_code == 404
    assert client.get(f"/api/_a/{clip_sha}.mp4").status_code == 404


# --- runtime files ----------------------------------------------------------------


def test_runtime_files_are_served_with_hash_caching(client, monkeypatch, tmp_path):
    (tmp_path / "fonts").mkdir()
    (tmp_path / "runtime.js").write_text("console.log('rt')")
    (tmp_path / "stage.css").write_text(".stage{}")
    (tmp_path / "fonts" / "a.woff2").write_bytes(b"wOF2")
    (tmp_path / ".hidden").write_text("secret")
    runtime = decks.load_runtime(tmp_path)
    assert len(runtime.hash) == 12 and set(runtime.files) == {
        "runtime.js",
        "stage.css",
        "fonts/a.woff2",
    }
    assert decks.load_runtime(tmp_path).hash == runtime.hash
    assert decks.load_runtime(tmp_path / "missing").files == {}
    monkeypatch.setattr(decks, "RUNTIME", runtime)

    slug, _files = _new_deck(client)
    assert f"/{slug}/_rt/{runtime.hash}/runtime.js" in client.get(f"/{slug}").text

    js = client.get(f"/{slug}/_rt/{runtime.hash}/runtime.js")
    assert js.status_code == 200 and js.text == "console.log('rt')"
    assert js.headers["content-type"].startswith("text/javascript")
    assert js.headers["cache-control"] == "public, max-age=31536000, immutable"
    font = client.get(f"/{slug}/_rt/{runtime.hash}/fonts/a.woff2")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2"
    stale = client.get(f"/{slug}/_rt/000000000000/stage.css")
    assert stale.status_code == 200 and stale.headers["cache-control"] == "no-cache"

    assert client.get(f"/{slug}/_rt/{runtime.hash}/theme-neutral.css").status_code == 404
    assert client.get(f"/{slug}/_rt/{runtime.hash}/.hidden").status_code == 404
    assert client.get(f"/{slug}/_rt/{runtime.hash}/fonts/..%2F..%2Fdb.py").status_code == 404
    assert client.get(f"/{slug}/_rt/{runtime.hash}/%2e%2e/decks.py").status_code == 404
    assert client.get(f"/api/_rt/{runtime.hash}/runtime.js").status_code == 404


# --- ownership and slug conflicts --------------------------------------------------


def test_owner_token_rules_match_html_pages(client):
    alice, bob = f"alice-{_uid()}", f"bob-{_uid()}"
    slug, files = _new_deck(client, token=alice)
    deck = _deck()

    forbidden = _push(client, slug, deck, files, token=bob)
    assert forbidden.status_code == 403 and "belongs to someone else" in forbidden.json()["detail"]
    assert _push(client, slug, deck, files).status_code == 403
    assert client.delete(f"/api/presentations/{slug}", headers=_headers(bob)).status_code == 403
    assert _push(client, slug, deck, files, token=alice).json()["version"] == 2

    # Missing owner tokens cannot create or take over decks.
    legacy = f"legacy-{_uid()}"
    assert _push(client, legacy, deck, files, token="").status_code == 400
    assert _push(client, legacy, deck, files, token=bob).status_code == 201
    assert _push(client, legacy, deck, files, token="").status_code == 400


def test_html_page_and_deck_never_overwrite_each_other(client):
    html_slug = f"page-{_uid()}"
    assert _upload_html(client, "<title>Page</title>", slug=html_slug).status_code == 201
    slug, files = _new_deck(client)

    conflict = _push(client, html_slug, _deck(), files)
    assert conflict.status_code == 409 and "HTML page" in conflict.json()["detail"]
    assert "Page" in client.get(f"/{html_slug}").text

    overwrite = _upload_html(client, "<title>Oops</title>", slug=slug)
    assert overwrite.status_code == 409 and "deck" in overwrite.json()["detail"]
    assert client.get(f"/{slug}").headers["etag"].startswith(f'"{slug}-v1-')

    # A derived slug that collides with a deck gets a suffix, as with any page.
    derived = _upload_html(client, f"<title>{slug}</title>")
    assert derived.status_code == 201 and derived.json()["slug"] == f"{slug}-2"

    assert _push(client, "api", _deck(), files).status_code == 400
    assert _push(client, "Bad_Slug", _deck(), files).status_code == 400


def test_library_and_owner_list_report_kind(client):
    token = f"owner-{_uid()}"
    slug, files = _new_deck(client, token=token)
    _push(client, slug, _deck(), files, token=token)
    html_slug = _upload_html(client, f"<title>Html {_uid()}</title>", token=token).json()["slug"]

    items = {item["slug"]: item for item in client.get("/api/library").json()["items"]}
    assert items[slug]["kind"] == "deck" and items[slug]["version"] == 2
    assert items[slug]["title"] == _deck()["title"] and items[slug]["path"] == f"/{slug}"
    assert items[slug]["size_bytes"] > 0
    assert items[html_slug]["kind"] == "html" and "version" not in items[html_slug]

    mine = {
        item["slug"]: item
        for item in client.get("/api/presentations", headers=_headers(token)).json()
    }
    assert mine[slug]["kind"] == "deck" and mine[slug]["version"] == 2
    assert mine[html_slug]["kind"] == "html" and "version" not in mine[html_slug]


def test_delete_removes_versions_but_keeps_assets(client):
    token = f"owner-{_uid()}"
    slug, files = _new_deck(client, token=token)
    old_etag = client.get(f"/{slug}").headers["etag"]
    assert client.delete(f"/api/presentations/{slug}", headers=_headers(token)).status_code == 204

    assert client.get(f"/{slug}").status_code == 404
    assert client.get(f"/{slug}/v1").status_code == 404
    assert client.get(f"/api/decks/{slug}/versions").status_code == 404
    assert client.get(f"/{slug}/_a/{files['assets/bg.jpg']}.jpg").status_code == 404
    with SessionLocal() as db:
        count = db.execute(
            select(func.count()).select_from(DeckVersion).where(DeckVersion.slug == slug)
        ).scalar()
        assert count == 0
        assert db.get(Asset, files["assets/bg.jpg"]) is not None

    # Re-created at the same slug: version 1 again, never the cached old wrapper.
    deck = _deck(title="Reborn")
    again = _push(client, slug, deck, files, token=f"other-{_uid()}")
    assert again.status_code == 201 and again.json()["version"] == 1
    page = client.get(f"/{slug}")
    assert "<title>Reborn</title>" in page.text and page.headers["etag"] != old_etag


def test_deck_and_html_asset_are_sandboxed(client):
    slug, files = _new_deck(client)
    pinned = client.get(f"/{slug}/v1")
    assert pinned.headers["content-security-policy"] == "sandbox allow-scripts"
    extra = (
        b'<section class="slide" data-slide="custom">Asset</section>'
        b"<script>localStorage.clear()</script>"
    )
    sha = _sha(extra)
    assert _put(client, extra, "text/html").status_code in (200, 201)
    # Assets must be referenced by a deck version before they can be served.
    assert client.get(f"/{slug}/_a/{sha}.html").status_code == 404
    deck = _deck(
        css=[], scripts=[], slides=[{"id": "custom", "layout": "html", "html": "slides/test.html"}]
    )
    new_files = {"deck.json": files["deck.json"], "slides/test.html": sha}
    assert _push(client, slug, deck, new_files).status_code == 201
    asset = client.get(f"/{slug}/_a/{sha}.html")
    assert asset.status_code == 200
    assert asset.headers["content-security-policy"] == "sandbox allow-scripts"


# --- validation ---------------------------------------------------------------------


def _slide(**props):
    return {"id": "s", **props}


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        (lambda d: d.update(schema=2), '"schema" must be 1'),
        (lambda d: d.update(schema=True), '"schema" must be 1'),
        (lambda d: d.pop("title"), '"title" is required'),
        (lambda d: d.update(title="  "), '"title" must not be empty'),
        (lambda d: d.update(title="x" * 301), "at most 300"),
        (lambda d: d.update(theme="dark"), '"theme" must be'),
        (lambda d: d.update(css=["theme/missing.css"]), "not in the bundle"),
        (lambda d: d.update(scripts="theme/extra.js"), '"scripts" must be a list'),
        (lambda d: d.update(flags={"x": "yes"}), '"flags" must be'),
        (lambda d: d.update(fine=3), '"fine" must be text'),
        (lambda d: d.update(slides=[]), '"slides" must be a list of 1-200'),
        (lambda d: d.update(slides=[_slide(layout="title", title="t")] * 2), "more than once"),
        (lambda d: d.update(slides=[{"id": "Bad ID", "layout": "title"}]), '"id" must be'),
        (lambda d: d.update(slides=[_slide(layout="wat")]), '"layout" must be one of'),
        (lambda d: d.update(slides=[_slide(layout="title")]), '("s"): "title" is required'),
        (lambda d: d.update(slides=[_slide(layout="statement", title=3)]), "must be text"),
        (lambda d: d.update(slides=[_slide(layout="hero-media", title="t")]), '"media" is req'),
        (
            lambda d: d.update(slides=[_slide(layout="split", title="t", media="assets/no.jpg")]),
            '"assets/no.jpg", which is not in the bundle',
        ),
        (
            lambda d: d.update(
                slides=[_slide(layout="split", title="t", media="assets/bg.jpg", side="up")]
            ),
            '"side" must be',
        ),
        (
            lambda d: d.update(slides=[_slide(layout="stats", title="t", stats=[])]),
            '"stats" must be a list of 1-4',
        ),
        (
            lambda d: d.update(slides=[_slide(layout="stats", title="t", stats=[{"value": "1"}])]),
            'stats[0]: "label" is required',
        ),
        (
            lambda d: d.update(slides=[_slide(layout="steps", title="t", steps=[{"title": "a"}])]),
            '"steps" must be a list of 2-6',
        ),
        (
            lambda d: d.update(slides=[_slide(layout="gallery", items=[{"caption": "c"}])]),
            'items[0]: "media" is required',
        ),
        (lambda d: d.update(slides=[_slide(layout="quote")]), '"quote" is required'),
        (lambda d: d.update(slides=[_slide(layout="media", media={"src": 3})]), "bundle path"),
        (
            lambda d: d.update(
                slides=[_slide(layout="media", media={"src": "assets/bg.jpg", "fit": "fill"})]
            ),
            '"media.fit" must be',
        ),
        (
            lambda d: d.update(
                slides=[_slide(layout="close", title="t", points=["a", "b", "c", "d", "e"])]
            ),
            '"points" must be a list of 0-4',
        ),
        (lambda d: d.update(slides=[_slide(layout="html")]), '"html" is required'),
        (
            lambda d: d.update(slides=[_slide(layout="html", html="slides/missing.html")]),
            "not in the bundle",
        ),
        (lambda d: d.update(slides=[_slide(layout="statement", title="t", tone="x")]), '"tone"'),
        (lambda d: d.update(slides=[_slide(layout="statement", title="t", fine=True)]), '"fine"'),
        (lambda d: d.update(slides=[_slide(layout="statement", title="t", when="")]), '"when"'),
        (
            lambda d: d.update(slides=[_slide(layout="statement", title="t", backup="y")]),
            '"backup" must be',
        ),
    ],
)
def test_deck_validation_errors_are_readable_400s(client, change, detail):
    files = _store(client, _bundle_bytes("validation"))
    deck = _deck()
    change(deck)
    response = _push(client, f"invalid-{_uid()}", deck, files)
    assert response.status_code == 400, response.text
    assert detail in response.json()["detail"]


def test_push_body_and_files_validation(client):
    files = _store(client, _bundle_bytes("validation"))
    slug = f"invalid-{_uid()}"
    cases = [
        ({"deck": _deck(), "files": {}}, '"files" must be a non-empty object'),
        ({"deck": _deck(), "files": {"../x": "0" * 64}}, "not a valid bundle path"),
        ({"deck": _deck(), "files": {"/abs": "0" * 64}}, "not a valid bundle path"),
        ({"deck": _deck(), "files": {"a b": "0" * 64}}, "not a valid bundle path"),
        ({"deck": _deck(), "files": {"x": "nothex"}}, "64-character hex sha256"),
        ({"deck": [], "files": files}, '"deck" must be the deck.json object'),
        ({"deck": _deck(), "files": files, "message": 5}, '"message" must be text'),
        ([], "Body must be a JSON object"),
    ]
    for body, detail in cases:
        response = client.post(f"/api/decks/{slug}/versions", json=body)
        assert response.status_code == 400, (body, response.text)
        assert detail in response.json()["detail"], response.json()

    unknown = {**files, "assets/extra.bin": _sha(f"never-{_uid()}".encode())}
    missing = _push(client, slug, _deck(), unknown)
    assert missing.status_code == 400
    assert "not uploaded yet (assets/extra.bin)" in missing.json()["detail"]
    assert client.get(f"/{slug}").status_code == 404


def test_idempotent_push_replays_the_same_version(client):
    files = _store(client, _bundle_bytes(_uid()))
    slug = f"idem-{_uid()}"
    headers = {"X-Idempotency-Key": f"key-{_uid()}", "X-Owner-Token": "owner-idempotent"}
    body = {"deck": _deck(), "files": files}
    first = client.post(f"/api/decks/{slug}/versions", json=body, headers=headers)
    again = client.post(f"/api/decks/{slug}/versions", json=body, headers=headers)
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json() and first.json()["version"] == 1


# --- migration ----------------------------------------------------------------------


def test_migrate_adds_kind_and_latest_version_to_a_legacy_table():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE presentations (slug VARCHAR(64) PRIMARY KEY, title VARCHAR(300), "
                "html TEXT, size_bytes INTEGER, created_at DATETIME, updated_at DATETIME)"
            )
        )
        conn.execute(text("INSERT INTO presentations (slug, html, size_bytes) VALUES ('a', '', 0)"))
    _migrate(engine)
    _migrate(engine)  # idempotent
    columns = {c["name"] for c in inspect(engine).get_columns("presentations")}
    assert {"owner_hash", "kind", "latest_version"} <= columns
    with engine.begin() as conn:
        row = conn.execute(text("SELECT kind, latest_version FROM presentations")).one()
    assert tuple(row) == ("html", None)


def test_head_works_on_latest_and_pinned_deck_pages(client):
    tag = _uid()
    files = _store(client, _bundle_bytes(tag))
    slug = f"deck-{tag}"
    assert _push(client, slug, _deck(), files, token=f"owner-{tag}").status_code == 201
    for path in (f"/{slug}", f"/{slug}/v1"):
        head = client.head(path)
        assert head.status_code == 200, (path, head.status_code)
        assert head.headers["content-type"].startswith("text/html")
