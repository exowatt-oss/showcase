"""Deck bundles: content-addressed assets, versioned deck.json, and the viewer wrapper.

The bundle format is described in ``README.md``. A deck is a row in
``presentations`` with ``kind = "deck"`` (so the library, ownership, shares and
delete keep working); its versions live in ``deck_versions`` and every bundle
file is stored once in ``assets`` by the SHA-256 of its bytes.

All database work goes through ``app.main._run_db`` and ``run_transaction`` in
the bounded worker pool. ``app.main`` is imported lazily (``_m()``) because
``app.main`` includes this module's router.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import LargeBinary, func, select

from app.db import (
    Asset,
    DeckVersion,
    Presentation,
    Reconciliation,
    RouteError,
    SessionLocal,
    TransactionResult,
    run_transaction,
)
from app.slugs import RESERVED_SLUGS, SLUG_RE, validate_slug

logger = logging.getLogger(__name__)
router = APIRouter()

KIND_HTML = "html"
KIND_DECK = "deck"

MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_VERSION_BODY_BYTES = 8 * 1024 * 1024
MAX_MISSING_BODY_BYTES = 2 * 1024 * 1024
MAX_MISSING_HASHES = 10_000
MAX_BUNDLE_FILES = 5_000
MAX_MESSAGE_CHARS = 2_000
MAX_TITLE_CHARS = 300
MAX_SLIDES = 200
IN_CLAUSE_CHUNK = 500
WRAPPER_CACHE_SIZE = 64

IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
BUNDLE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SLIDE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_EXT_RE = re.compile(r"^[a-z0-9]{1,10}$")
_MIME_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_DIGITS_RE = re.compile(r"^[0-9]+$")

THEMES = ("neutral", "none")
TONES = ("dark", "light", "paper", "bright", "dusk")
FITS = ("cover", "contain")
SIDES = ("left", "right")

_TEXT_MIME = {
    "css": "text/css; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "mjs": "text/javascript; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
    "map": "application/json",
}
_MIME_BY_EXT = {
    **_TEXT_MIME,
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "svg": "image/svg+xml",
    "ico": "image/x-icon",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "woff2": "font/woff2",
    "woff": "font/woff",
    "ttf": "font/ttf",
    "otf": "font/otf",
    "pdf": "application/pdf",
    "wasm": "application/wasm",
    "glb": "model/gltf-binary",
    "gltf": "model/gltf+json",
}


def _m():
    """``app.main``, imported lazily (it includes this router)."""
    from app import main

    return main


def mime_for_ext(ext: str) -> str | None:
    return _MIME_BY_EXT.get(ext.lower())


def ext_of(path: str) -> str:
    """Lowercase extension of a bundle path's last segment, or ``bin``."""
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return "bin"
    ext = name.rsplit(".", 1)[-1].lower()
    return ext if _EXT_RE.fullmatch(ext) else "bin"


def asset_url(slug: str, sha256: str, path: str) -> str:
    return f"/{slug}/_a/{sha256}.{ext_of(path)}"


def _chunks(items: list[str], size: int = IN_CLAUSE_CHUNK) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --- Viewer runtime (src/app/static/runtime) ---------------------------------

RUNTIME_DIR = Path(__file__).parent / "static" / "runtime"


@dataclass(frozen=True, slots=True)
class Runtime:
    """The viewer runtime's files, loaded once, and their combined short hash."""

    hash: str
    files: dict[str, bytes]
    etags: dict[str, str]


def load_runtime(directory: Path = RUNTIME_DIR) -> Runtime:
    """Read every (non-dot) file under ``directory``; a missing dir is an empty runtime."""
    files: dict[str, bytes] = {}
    etags: dict[str, str] = {}
    digest = hashlib.sha256()
    if directory.is_dir():
        found = []
        for path in directory.rglob("*"):
            rel = path.relative_to(directory).as_posix()
            if path.is_file() and not any(part.startswith(".") for part in rel.split("/")):
                found.append((rel, path))
        for rel, path in sorted(found):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            files[rel] = data
            file_hash = hashlib.sha256(data).hexdigest()
            etags[rel] = f'"{file_hash[:16]}"'
            digest.update(rel.encode())
            digest.update(b"\0")
            digest.update(file_hash.encode())
            digest.update(b"\0")
    return Runtime(digest.hexdigest()[:12], files, etags)


RUNTIME = load_runtime()


# --- Rendered-wrapper LRU -------------------------------------------------------


class _LRU:
    def __init__(self, size: int):
        self._size = size
        self._items: OrderedDict[tuple, str] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> str | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: tuple, value: str) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._size:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


WRAPPERS = _LRU(WRAPPER_CACHE_SIZE)


# --- deck.json validation -------------------------------------------------------


class DeckInputError(ValueError):
    """A user-correctable problem with a pushed version (HTTP 400)."""


@dataclass(frozen=True, slots=True)
class LayoutSpec:
    required_text: tuple[str, ...] = ()
    text: tuple[str, ...] = ()
    required_media: tuple[str, ...] = ()
    media: tuple[str, ...] = ()
    side: bool = False
    stats: bool = False
    steps: bool = False
    items: bool = False
    points: bool = False
    html: bool = False


LAYOUTS: dict[str, LayoutSpec] = {
    "title": LayoutSpec(required_text=("title",), text=("kicker", "subtitle"), media=("media",)),
    "statement": LayoutSpec(required_text=("title",), text=("kicker", "body")),
    "hero-media": LayoutSpec(
        required_text=("title",), required_media=("media",), text=("kicker", "body"), side=True
    ),
    "split": LayoutSpec(
        required_text=("title",), required_media=("media",), text=("kicker", "body"), side=True
    ),
    "stats": LayoutSpec(
        required_text=("title",), text=("kicker", "body"), media=("media",), stats=True
    ),
    "steps": LayoutSpec(required_text=("title",), text=("kicker",), steps=True),
    "gallery": LayoutSpec(text=("title", "kicker"), items=True),
    "quote": LayoutSpec(required_text=("quote",), text=("by",), media=("media",)),
    "media": LayoutSpec(required_media=("media",), text=("caption",)),
    "close": LayoutSpec(required_text=("title",), text=("kicker",), media=("media",), points=True),
    "html": LayoutSpec(html=True),
}


def _present(obj: dict, key: str) -> bool:
    return key in obj and obj[key] is not None


def _check_text(obj: dict, key: str, where: str, *, required: bool = False) -> None:
    if not _present(obj, key):
        if required:
            raise DeckInputError(f'{where}: "{key}" is required.')
        return
    value = obj[key]
    if not isinstance(value, str):
        raise DeckInputError(f'{where}: "{key}" must be text.')
    if required and not value.strip():
        raise DeckInputError(f'{where}: "{key}" must not be empty.')


def _check_path(value: Any, where: str, key: str, files: dict[str, str]) -> None:
    if not isinstance(value, str) or not value:
        raise DeckInputError(f'{where}: "{key}" must be a bundle path.')
    if value not in files:
        raise DeckInputError(
            f'{where}: "{key}" refers to "{value[:200]}", which is not in the bundle\'s files.'
        )


def _check_media(obj: dict, key: str, where: str, files: dict[str, str], *, required: bool):
    if not _present(obj, key):
        if required:
            raise DeckInputError(f'{where}: "{key}" is required.')
        return
    value = obj[key]
    if isinstance(value, str):
        _check_path(value, where, key, files)
        return
    if not isinstance(value, dict):
        raise DeckInputError(
            f'{where}: "{key}" must be a bundle path or a {{"src": …}} media object.'
        )
    _check_path(value.get("src"), where, f"{key}.src", files)
    if _present(value, "poster"):
        _check_path(value["poster"], where, f"{key}.poster", files)
    _check_text(value, "alt", f'{where} "{key}"')
    if _present(value, "fit") and value["fit"] not in FITS:
        raise DeckInputError(f'{where}: "{key}.fit" must be "cover" or "contain".')
    if _present(value, "loop") and not isinstance(value["loop"], bool):
        raise DeckInputError(f'{where}: "{key}.loop" must be true or false.')


def _check_list(obj: dict, key: str, where: str, low: int, high: int, *, required: bool) -> list:
    if not _present(obj, key):
        if required:
            raise DeckInputError(f'{where}: "{key}" is required ({low}-{high} items).')
        return []
    value = obj[key]
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise DeckInputError(f'{where}: "{key}" must be a list of {low}-{high} items.')
    return value


def _check_entries(
    entries: list,
    key: str,
    where: str,
    files: dict[str, str],
    *,
    required_text: tuple[str, ...] = (),
    text: tuple[str, ...] = (),
    media: tuple[str, ...] = (),
    req_media: tuple[str, ...] = (),
) -> None:
    for n, entry in enumerate(entries):
        at = f"{where} {key}[{n}]"
        if not isinstance(entry, dict):
            raise DeckInputError(f"{at}: must be an object.")
        for name in required_text:
            _check_text(entry, name, at, required=True)
        for name in text:
            _check_text(entry, name, at)
        for name in req_media:
            _check_media(entry, name, at, files, required=True)
        for name in media:
            _check_media(entry, name, at, files, required=False)


def _slide_where(index: int, slide: Any) -> str:
    sid = slide.get("id") if isinstance(slide, dict) else None
    if isinstance(sid, str) and SLIDE_ID_RE.fullmatch(sid):
        return f'slides[{index}] ("{sid}")'
    return f"slides[{index}]"


def _check_slide(index: int, slide: Any, files: dict[str, str], seen: set[str]) -> None:
    where = _slide_where(index, slide)
    if not isinstance(slide, dict):
        raise DeckInputError(f"{where}: each slide must be an object.")
    sid = slide.get("id")
    if not isinstance(sid, str) or not SLIDE_ID_RE.fullmatch(sid):
        raise DeckInputError(
            f'{where}: "id" must be 1-64 characters of lowercase letters, digits and '
            "hyphens, starting with a letter or digit."
        )
    if sid in seen:
        raise DeckInputError(f'{where}: slide id "{sid}" is used more than once.')
    seen.add(sid)
    layout = slide.get("layout")
    spec = LAYOUTS.get(layout) if isinstance(layout, str) else None
    if spec is None:
        raise DeckInputError(f'{where}: "layout" must be one of: {", ".join(LAYOUTS)}.')
    _check_text(slide, "notes", where)
    if _present(slide, "backup") and not isinstance(slide["backup"], bool):
        raise DeckInputError(f'{where}: "backup" must be true or false.')
    if _present(slide, "when"):
        when = slide["when"]
        if not isinstance(when, str) or not when.removeprefix("!").strip():
            raise DeckInputError(f'{where}: "when" must be a flag name (or "!name").')
    if _present(slide, "tone") and slide["tone"] not in TONES:
        raise DeckInputError(f'{where}: "tone" must be one of: {", ".join(TONES)}.')
    if _present(slide, "fine") and not (isinstance(slide["fine"], str) or slide["fine"] is False):
        raise DeckInputError(f'{where}: "fine" must be text or false.')

    for key in spec.required_text:
        _check_text(slide, key, where, required=True)
    for key in spec.text:
        _check_text(slide, key, where)
    for key in spec.required_media:
        _check_media(slide, key, where, files, required=True)
    for key in spec.media:
        _check_media(slide, key, where, files, required=False)
    if spec.side and _present(slide, "side") and slide["side"] not in SIDES:
        raise DeckInputError(f'{where}: "side" must be "left" or "right".')
    if spec.stats:
        entries = _check_list(slide, "stats", where, 1, 4, required=True)
        _check_entries(
            entries, "stats", where, files, required_text=("value", "label"), text=("sub",)
        )
    if spec.steps:
        entries = _check_list(slide, "steps", where, 2, 6, required=True)
        _check_entries(
            entries,
            "steps",
            where,
            files,
            required_text=("title",),
            text=("body",),
            media=("media",),
        )
    if spec.items:
        entries = _check_list(slide, "items", where, 1, 8, required=True)
        _check_entries(entries, "items", where, files, text=("caption",), req_media=("media",))
    if spec.points:
        points = _check_list(slide, "points", where, 0, 4, required=False)
        if not all(isinstance(point, str) for point in points):
            raise DeckInputError(f'{where}: "points" must be a list of text.')
    if spec.html:
        if not _present(slide, "html"):
            raise DeckInputError(f'{where}: "html" is required (the slide file\'s bundle path).')
        _check_path(slide["html"], where, "html", files)


def validate_files(files: Any) -> dict[str, str]:
    """The ``files`` map: bundle path -> lowercase sha256, paths per the contract."""
    if not isinstance(files, dict) or not files:
        raise DeckInputError('"files" must be a non-empty object of bundle path -> sha256.')
    if len(files) > MAX_BUNDLE_FILES:
        raise DeckInputError(f"A bundle can hold at most {MAX_BUNDLE_FILES} files.")
    out: dict[str, str] = {}
    for path, sha in files.items():
        shown = str(path)[:200]
        segments = path.split("/") if isinstance(path, str) else []
        if (
            not isinstance(path, str)
            or not BUNDLE_PATH_RE.fullmatch(path)
            or any(segment in ("", ".", "..") for segment in segments)
        ):
            raise DeckInputError(
                f'files: "{shown}" is not a valid bundle path (relative, [A-Za-z0-9._/-], '
                'no "..", at most 200 characters).'
            )
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha.lower()):
            raise DeckInputError(f'files: "{shown}" must map to a 64-character hex sha256.')
        out[path] = sha.lower()
    return out


def validate_deck(deck: Any, files: dict[str, str]) -> None:
    if not isinstance(deck, dict):
        raise DeckInputError('"deck" must be the deck.json object.')
    schema = deck.get("schema")
    if type(schema) is not int or schema != 1:
        raise DeckInputError('deck.json: "schema" must be 1.')
    _check_text(deck, "title", "deck.json", required=True)
    if len(deck["title"]) > MAX_TITLE_CHARS:
        raise DeckInputError(f'deck.json: "title" must be at most {MAX_TITLE_CHARS} characters.')
    if _present(deck, "theme") and deck["theme"] not in THEMES:
        raise DeckInputError('deck.json: "theme" must be "neutral" or "none".')
    for key in ("css", "scripts"):
        if not _present(deck, key):
            continue
        paths = deck[key]
        if not isinstance(paths, list):
            raise DeckInputError(f'deck.json: "{key}" must be a list of bundle paths.')
        for n, path in enumerate(paths):
            _check_path(path, "deck.json", f"{key}[{n}]", files)
    if _present(deck, "flags"):
        flags = deck["flags"]
        if not isinstance(flags, dict) or not all(
            isinstance(name, str) and isinstance(value, bool) for name, value in flags.items()
        ):
            raise DeckInputError('deck.json: "flags" must be an object of name -> true/false.')
    _check_text(deck, "fine", "deck.json")
    slides = deck.get("slides")
    if not isinstance(slides, list) or not 1 <= len(slides) <= MAX_SLIDES:
        raise DeckInputError(f'deck.json: "slides" must be a list of 1-{MAX_SLIDES} slides.')
    seen: set[str] = set()
    for index, slide in enumerate(slides):
        _check_slide(index, slide, files, seen)


def validate_push(payload: Any) -> tuple[dict, dict[str, str], str]:
    """Validate a ``POST /api/decks/{slug}/versions`` body; return (deck, files, message)."""
    if not isinstance(payload, dict):
        raise DeckInputError('Body must be a JSON object: {"deck": …, "files": …, "message": …}.')
    files = validate_files(payload.get("files"))
    deck = payload.get("deck")
    validate_deck(deck, files)
    message = payload.get("message")
    if message is None:
        message = ""
    if not isinstance(message, str):
        raise DeckInputError('"message" must be text.')
    if len(message) > MAX_MESSAGE_CHARS:
        raise DeckInputError(f'"message" must be at most {MAX_MESSAGE_CHARS} characters.')
    return deck, files, message


# --- Asset-reference rewriting and the wrapper ----------------------------------

_SKIP_REWRITE_KEYS = frozenset({"id", "layout", "when", "tone", "side", "fit"})


class Rewriter:
    """Rewrite bundle paths (in the file map, not preceded by [A-Za-z0-9_./-]) to asset URLs."""

    def __init__(self, slug: str, files: dict[str, str]):
        self.urls = {path: asset_url(slug, sha, path) for path, sha in files.items()}
        self.pattern = None
        if self.urls:
            alternation = "|".join(
                re.escape(path) for path in sorted(self.urls, key=len, reverse=True)
            )
            self.pattern = re.compile(rf"(?<![A-Za-z0-9_./-])(?:{alternation})")

    def text(self, value: str) -> str:
        if self.pattern is None:
            return value
        return self.pattern.sub(lambda match: self.urls[match.group(0)], value)

    def value(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, str):
            return value if key in _SKIP_REWRITE_KEYS else self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {k: self.value(v, k) for k, v in value.items()}
        return value


def _escape_json_for_script(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


_STYLE_CLOSE_RE = re.compile(r"</(style)", re.IGNORECASE)


def render_wrapper(
    *,
    slug: str,
    version: int,
    deck: dict,
    files: dict[str, str],
    texts: dict[str, str],
    runtime_hash: str,
) -> str:
    """The viewer page: theme + deck CSS, the resolved deck as JSON, the runtime script."""
    rewriter = Rewriter(slug, files)

    def file_text(path: str) -> str:
        return rewriter.text(texts.get(files.get(path, ""), ""))

    theme = deck.get("theme") or "neutral"
    slides = []
    for slide in deck["slides"]:
        resolved = rewriter.value(slide)
        if slide.get("layout") == "html":
            resolved["source"] = file_text(slide["html"])
        slides.append(resolved)
    data = {
        "slug": slug,
        "version": version,
        "title": deck["title"],
        "theme": theme,
        "flags": deck.get("flags") or {},
        "fine": deck.get("fine") or "",
        "slides": slides,
        "scriptSources": [file_text(path) for path in deck.get("scripts") or []],
    }
    css = "\n".join(file_text(path) for path in deck.get("css") or [])
    css = _STYLE_CLOSE_RE.sub(r"<\\/\1", css)
    rt = f"/{slug}/_rt/{runtime_hash}"
    theme_link = (
        f'<link rel="stylesheet" href="{rt}/theme-neutral.css">\n' if theme == "neutral" else ""
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html_lib.escape(deck['title'])}</title>\n"
        f'<link rel="stylesheet" href="{rt}/stage.css">\n'
        f"{theme_link}"
        f'<style id="deck-css">{css}</style>\n'
        "</head>\n"
        "<body>\n"
        '<div id="deck-root"></div>\n'
        f'<script type="application/json" id="deck-data">{_escape_json_for_script(data)}</script>\n'
        f'<script src="{rt}/runtime.js" defer></script>\n'
        "</body>\n"
        "</html>\n"
    )


def _generation(created_at: datetime | None) -> str:
    """A short tag that changes when a slug is deleted and re-created."""
    if created_at is None:
        return "0"
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    stamp = created_at.astimezone(UTC).isoformat()
    return hashlib.sha256(stamp.encode()).hexdigest()[:8]


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == etag:
            return True
    return False


@dataclass(slots=True)
class DeckPage:
    """A deck page ready to send; ``html is None`` means 304 Not Modified."""

    slug: str
    version: int
    etag: str
    pinned: bool
    html: str | None


@dataclass(slots=True)
class DeckSource:
    """Everything needed to render a wrapper, loaded inside the transaction."""

    page: DeckPage
    cache_key: tuple
    deck: dict
    files: dict[str, str]
    texts: dict[str, str]
    runtime_hash: str


def load_deck_page(
    db, row: Any, version: int | None, if_none_match: str | None
) -> DeckPage | DeckSource:
    """Inside a transaction: resolve a deck page to a 304, a cached page, or its source.

    ``row`` is a ``presentations`` row (ORM object or Row) with ``kind == "deck"``;
    ``version`` is ``None`` for the latest version.
    """
    runtime_hash = RUNTIME.hash
    latest = row.latest_version or 0
    pinned = version is not None
    number = version if pinned else latest
    if not number or not 1 <= number <= latest:
        raise RouteError(404, f"/{row.slug} has no version {number}." if pinned else "No deck.")
    generation = _generation(row.created_at)
    etag = f'"{row.slug}-v{number}-{runtime_hash}-{generation}"'
    if etag_matches(if_none_match, etag):
        return DeckPage(row.slug, number, etag, pinned, None)
    key = (row.slug, number, runtime_hash, generation)
    cached = WRAPPERS.get(key)
    if cached is not None:
        return DeckPage(row.slug, number, etag, pinned, cached)
    stored = db.execute(
        select(DeckVersion.deck_json, DeckVersion.files_json).where(
            DeckVersion.slug == row.slug, DeckVersion.version == number
        )
    ).first()
    if stored is None:
        raise RouteError(404, f"/{row.slug} has no version {number}.")
    deck = json.loads(stored.deck_json)
    files = json.loads(stored.files_json)
    text_paths = [slide["html"] for slide in deck["slides"] if slide.get("layout") == "html"]
    text_paths += list(deck.get("css") or []) + list(deck.get("scripts") or [])
    wanted = sorted({files[path] for path in text_paths if path in files})
    texts: dict[str, str] = {}
    for chunk in _chunks(wanted):
        for sha, data in db.execute(
            select(Asset.sha256, Asset.data).where(Asset.sha256.in_(chunk))
        ).all():
            texts[sha] = bytes(data).decode("utf-8", errors="replace")
    page = DeckPage(row.slug, number, etag, pinned, None)
    return DeckSource(page, key, deck, files, texts, runtime_hash)


def finish(result: TransactionResult) -> TransactionResult:
    """After the transaction (still in the worker): render a loaded ``DeckSource``."""
    source = result.value
    if not isinstance(source, DeckSource):
        return result
    html = render_wrapper(
        slug=source.page.slug,
        version=source.page.version,
        deck=source.deck,
        files=source.files,
        texts=source.texts,
        runtime_hash=source.runtime_hash,
    )
    WRAPPERS.put(source.cache_key, html)
    return replace(result, value=replace(source.page, html=html))


def page_response(page: DeckPage) -> Response:
    headers = {"ETag": page.etag, "Cache-Control": IMMUTABLE if page.pinned else NO_CACHE}
    if page.html is None:
        return Response(status_code=304, headers=headers)
    return HTMLResponse(page.html, headers=headers)


# --- Request helpers --------------------------------------------------------------


async def _read_json(request: Request, limit: int) -> Any:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"Request body is larger than {limit // (1024 * 1024)} MB.")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, f"Request body is larger than {limit // (1024 * 1024)} MB.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Request body must be valid JSON.") from exc


def _servable_slug(slug: str) -> bool:
    return SLUG_RE.fullmatch(slug) is not None and slug not in RESERVED_SLUGS


def _deck_kind_or_404(db, slug: str) -> Any:
    row = db.execute(
        select(
            Presentation.slug,
            Presentation.kind,
            Presentation.created_at,
            Presentation.latest_version,
        ).where(Presentation.slug == slug)
    ).first()
    if row is None or row.kind != KIND_DECK:
        raise RouteError(404, f"No deck at /{slug}.")
    return row


# --- Assets API -------------------------------------------------------------------


@router.post(
    "/api/assets/missing",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"hashes": {"type": "array", "items": {"type": "string"}}},
                    },
                    "example": {"hashes": ["<sha256>"]},
                }
            },
        }
    },
)
async def missing_assets(request: Request):
    """Which of these sha256 hashes are not stored yet (upload those with PUT)."""
    payload = await _read_json(request, MAX_MISSING_BODY_BYTES)
    hashes = payload.get("hashes") if isinstance(payload, dict) else None
    if not isinstance(hashes, list):
        raise HTTPException(400, 'Body must be {"hashes": ["<sha256>", …]}.')
    if len(hashes) > MAX_MISSING_HASHES:
        raise HTTPException(400, f"Ask about at most {MAX_MISSING_HASHES} hashes at once.")
    wanted: list[str] = []
    for value in hashes:
        if not isinstance(value, str) or not SHA_RE.fullmatch(value.lower()):
            shown = str(value)[:80]
            raise HTTPException(400, f'"{shown}" is not a 64-character hex sha256.')
        if value.lower() not in wanted:
            wanted.append(value.lower())
    main = _m()
    operation_id = uuid.uuid4().hex

    def missing_transaction():
        def body(db):
            stored: set[str] = set()
            for chunk in _chunks(wanted):
                stored.update(
                    db.execute(select(Asset.sha256).where(Asset.sha256.in_(chunk))).scalars()
                )
            return [sha for sha in wanted if sha not in stored]

        return run_transaction(operation_id, body)

    result = await main._run_db(
        request,
        operation="asset_missing",
        callable_=missing_transaction,
        operation_id=operation_id,
    )
    return {"missing": result.value}


@router.put(
    "/api/assets/{sha256}",
    responses={200: {"description": "Already stored"}, 201: {"description": "Stored"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"*/*": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def put_asset(request: Request, sha256: str):
    """Store one bundle file (raw body) under its sha256; the server verifies the hash."""
    sha = sha256.lower()
    if not SHA_RE.fullmatch(sha):
        raise HTTPException(400, "The URL must end in the file's 64-character hex sha256.")
    too_big = f"File is larger than {MAX_ASSET_BYTES // (1024 * 1024)} MB."
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_ASSET_BYTES:
        raise HTTPException(413, too_big)
    digest = hashlib.sha256()
    parts: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_ASSET_BYTES:
            raise HTTPException(413, too_big)
        digest.update(chunk)
        parts.append(chunk)
    actual = digest.hexdigest()
    if actual != sha:
        raise HTTPException(
            400, f"The body's sha256 is {actual}, not {sha} — the upload was changed or cut short."
        )
    data = b"".join(parts)
    mime = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if not _MIME_RE.fullmatch(mime) or len(mime) > 100:
        mime = "application/octet-stream"
    main = _m()
    operation_id = uuid.uuid4().hex

    def reconcile():
        with SessionLocal() as db:
            if db.execute(select(Asset.sha256).where(Asset.sha256 == sha)).first():
                return Reconciliation(True, "committed")
            return Reconciliation(False, "rolled_back")

    def put_transaction():
        def body(db):
            if db.execute(select(Asset.sha256).where(Asset.sha256 == sha)).first():
                return False
            db.add(Asset(sha256=sha, mime=mime, size_bytes=len(data), data=data))
            return True

        return run_transaction(operation_id, body, reconcile=reconcile)

    result = await main._run_db(
        request,
        operation="asset_put",
        callable_=put_transaction,
        operation_id=operation_id,
        mutation=True,
    )
    if isinstance(result, main.UnknownWrite):
        return main._unknown_write_response(result)
    created = bool(result.value)
    if created:
        logger.info("asset_put operation=%s sha256=%s size_bytes=%d", operation_id, sha, len(data))
    return JSONResponse(
        status_code=201 if created else 200,
        content={"sha256": sha, "size_bytes": len(data), "created": created},
    )


# --- Deck versions API ------------------------------------------------------------

_PUSH_EXAMPLE = {
    "deck": {
        "schema": 1,
        "title": "My deck",
        "slides": [{"id": "opener", "layout": "title", "title": "Hello"}],
    },
    "files": {"deck.json": "<sha256>"},
    "message": "first version",
}


@router.post(
    "/api/decks/{slug}/versions",
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"type": "object"}, "example": _PUSH_EXAMPLE}
            },
        }
    },
)
async def push_version(
    request: Request,
    slug: str,
    x_owner_token: str = Header(""),
    x_idempotency_key: str | None = Header(None),
):
    """Publish a new version of a deck (creates the deck on first push)."""
    main = _m()
    if err := validate_slug(slug):
        raise HTTPException(400, err)
    payload = await _read_json(request, MAX_VERSION_BODY_BYTES)
    try:
        deck, files, message = validate_push(payload)
    except DeckInputError as exc:
        raise HTTPException(400, str(exc)) from exc
    caller = main._owner_hash(x_owner_token)
    if caller is None:
        raise HTTPException(400, "X-Owner-Token is required for deck uploads.")
    idempotency_key = main._validated_idempotency_key(x_idempotency_key)
    deck_json = json.dumps(deck, ensure_ascii=False, separators=(",", ":"))
    files_json = json.dumps(files, ensure_ascii=False, separators=(",", ":"))
    fingerprint = main._operation_fingerprint(
        "deck-push",
        caller or "",
        slug,
        hashlib.sha256(f"{deck_json}\0{files_json}\0{message}".encode()).hexdigest(),
    )
    operation_id = uuid.uuid4().hex
    context: dict[str, Any] = {"response": None}

    def reconcile():
        response = context["response"]
        if response is None:
            return None
        with SessionLocal() as db:
            row = db.get(DeckVersion, (slug, response["version"]))
            if row is None:
                return Reconciliation({}, "rolled_back")
            if row.deck_json == deck_json and row.files_json == files_json:
                return Reconciliation(response, "committed")
            return None

    def push_transaction():
        def body(db):
            existing = db.execute(
                select(Presentation).where(Presentation.slug == slug).with_for_update()
            ).scalar_one_or_none()
            if existing is not None and existing.kind != KIND_DECK:
                raise RouteError(
                    409,
                    f"/{slug} is a single-file HTML page, not a deck — pick another slug "
                    "(or delete that page first).",
                )
            if existing is not None and not main._may_modify(existing, caller):
                raise RouteError(
                    403,
                    f"/{slug} belongs to someone else — you can only push to decks you "
                    "created (with the same owner token). Pick a different slug.",
                )
            hashes = sorted(set(files.values()))
            sizes: dict[str, int] = {}
            for chunk in _chunks(hashes):
                sizes.update(
                    db.execute(
                        select(Asset.sha256, Asset.size_bytes).where(Asset.sha256.in_(chunk))
                    ).all()
                )
            missing = [path for path, sha in files.items() if sha not in sizes]
            if missing:
                shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
                raise RouteError(
                    400,
                    f"{len(missing)} file(s) are not uploaded yet ({shown}) — "
                    "PUT /api/assets/<sha256> first.",
                )
            size_bytes = sum(sizes[sha] for sha in files.values())
            highest = db.execute(
                select(func.max(DeckVersion.version)).where(DeckVersion.slug == slug)
            ).scalar()
            previous = existing.latest_version if existing is not None else None
            version = max(highest or 0, previous or 0) + 1
            db.add(
                DeckVersion(
                    slug=slug,
                    version=version,
                    deck_json=deck_json,
                    files_json=files_json,
                    message=message,
                    size_bytes=size_bytes,
                )
            )
            if existing is None:
                db.add(
                    Presentation(
                        slug=slug,
                        title=deck["title"],
                        html="",
                        size_bytes=size_bytes,
                        owner_hash=caller,
                        kind=KIND_DECK,
                        latest_version=version,
                    )
                )
            else:
                existing.title = deck["title"]
                existing.size_bytes = size_bytes
                existing.latest_version = version
                existing.owner_hash = caller
            response = {
                "slug": slug,
                "version": version,
                "url": f"/{slug}",
                "pinned_url": f"/{slug}/v{version}",
                "size_bytes": size_bytes,
            }
            context["response"] = response
            return response

        return run_transaction(operation_id, body, reconcile=reconcile)

    result = await main._run_db(
        request,
        operation="deck_push",
        callable_=push_transaction,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        kind="deck-push",
        fingerprint=fingerprint,
        mutation=True,
    )
    if isinstance(result, main.UnknownWrite):
        return main._unknown_write_response(result)
    value = result.value
    logger.info(
        "deck_push operation=%s outcome=%s slug=%s version=%d size_bytes=%d files=%d",
        result.operation_id,
        result.outcome,
        slug,
        value["version"],
        value["size_bytes"],
        len(files),
    )
    return value


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


@router.get("/api/decks/{slug}/versions")
async def list_versions(request: Request, slug: str):
    """Every version of a deck, newest first."""
    operation_id = uuid.uuid4().hex

    def list_transaction():
        def body(db):
            _deck_kind_or_404(db, slug)
            rows = db.execute(
                select(
                    DeckVersion.version,
                    DeckVersion.message,
                    DeckVersion.size_bytes,
                    DeckVersion.created_at,
                )
                .where(DeckVersion.slug == slug)
                .order_by(DeckVersion.version.desc())
            ).all()
            return [
                {
                    "version": row.version,
                    "message": row.message,
                    "size_bytes": row.size_bytes,
                    "created_at": _iso(row.created_at),
                }
                for row in rows
            ]

        return run_transaction(operation_id, body)

    result = await _m()._run_db(
        request,
        operation="deck_versions",
        callable_=list_transaction,
        operation_id=operation_id,
    )
    return result.value


@router.get("/api/decks/{slug}/versions/{version}")
async def get_version(request: Request, slug: str, version: int):
    """One version: its deck.json, file map and message."""
    operation_id = uuid.uuid4().hex

    def get_transaction():
        def body(db):
            _deck_kind_or_404(db, slug)
            row = db.execute(
                select(
                    DeckVersion.version,
                    DeckVersion.deck_json,
                    DeckVersion.files_json,
                    DeckVersion.message,
                    DeckVersion.created_at,
                ).where(DeckVersion.slug == slug, DeckVersion.version == version)
            ).first()
            if row is None:
                raise RouteError(404, f"/{slug} has no version {version}.")
            return {
                "version": row.version,
                "deck": json.loads(row.deck_json),
                "files": json.loads(row.files_json),
                "message": row.message,
                "created_at": _iso(row.created_at),
            }

        return run_transaction(operation_id, body)

    result = await _m()._run_db(
        request,
        operation="deck_version",
        callable_=get_transaction,
        operation_id=operation_id,
    )
    return result.value


# --- Serving: pinned versions, assets, runtime ------------------------------------


@router.api_route(
    "/{slug}/v{version:int}",
    methods=["GET", "HEAD"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def serve_pinned(request: Request, slug: str, version: int):
    if not _servable_slug(slug):
        raise HTTPException(404, "Not found.")
    main = _m()
    if_none_match = request.headers.get("if-none-match")
    operation_id = uuid.uuid4().hex

    def pinned_transaction():
        def body(db):
            row = _deck_kind_or_404(db, slug)
            return load_deck_page(db, row, version, if_none_match)

        return finish(run_transaction(operation_id, body))

    result = await main._run_db(
        request,
        operation="deck_serve_pinned",
        callable_=pinned_transaction,
        operation_id=operation_id,
    )
    response = page_response(result.value)
    return response


UNSATISFIABLE = "unsatisfiable"


def parse_range(header: str | None, size: int) -> tuple[int, int] | str | None:
    """A single ``bytes=`` range as inclusive (start, end); None = send everything."""
    if not header:
        return None
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes":
        return None
    spec = spec.strip()
    if "," in spec:
        return None  # multi-range: answer with the whole body (RFC 9110 allows it)
    first, dash, last = (part.strip() for part in spec.partition("-"))
    if not dash or size <= 0:
        return UNSATISFIABLE
    if first == "":
        if not _DIGITS_RE.fullmatch(last) or int(last) == 0:
            return UNSATISFIABLE
        return max(0, size - int(last)), size - 1
    if not _DIGITS_RE.fullmatch(first) or (last and not _DIGITS_RE.fullmatch(last)):
        return UNSATISFIABLE
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        return UNSATISFIABLE
    return start, min(end, size - 1)


@dataclass(slots=True)
class AssetHit:
    status: int
    size: int
    mime: str
    data: bytes = b""
    span: tuple[int, int] | None = None


@router.api_route("/{slug}/_a/{name}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_asset(request: Request, slug: str, name: str):
    sha, _, ext = name.partition(".")
    sha = sha.lower()
    if not _servable_slug(slug) or not SHA_RE.fullmatch(sha):
        raise HTTPException(404, "Not found.")
    etag = f'"{sha}"'
    head = request.method == "HEAD"
    range_header = request.headers.get("range")
    if_none_match = request.headers.get("if-none-match")
    operation_id = uuid.uuid4().hex

    def asset_transaction():
        def body(db):
            _deck_kind_or_404(db, slug)
            # Content-addressed storage is shared across decks, but a deck may
            # only expose assets referenced by one of *its own* versions.
            referenced = db.execute(
                select(DeckVersion.version)
                .where(DeckVersion.slug == slug, DeckVersion.files_json.like(f'%:"{sha}"%'))
                .limit(1)
            ).first()
            if referenced is None:
                raise RouteError(404, "No such file in this deck.")
            meta = db.execute(
                select(Asset.size_bytes, Asset.mime).where(Asset.sha256 == sha)
            ).first()
            if meta is None:
                raise RouteError(404, "No such file.")
            size, mime = int(meta.size_bytes), meta.mime
            if etag_matches(if_none_match, etag):
                return AssetHit(304, size, mime)
            span = parse_range(range_header, size)
            if span == UNSATISFIABLE:
                return AssetHit(416, size, mime)
            status = 200 if span is None else 206
            if head:
                return AssetHit(status, size, mime, span=span)
            if span is None:
                column = Asset.data
            else:
                start, end = span
                column = func.substr(Asset.data, start + 1, end - start + 1, type_=LargeBinary)
            data = db.execute(select(column).where(Asset.sha256 == sha)).scalar_one()
            return AssetHit(status, size, mime, bytes(data), span)

        return run_transaction(operation_id, body)

    result = await _m()._run_db(
        request,
        operation="deck_asset",
        callable_=asset_transaction,
        operation_id=operation_id,
    )
    hit: AssetHit = result.value
    headers = {
        "Cache-Control": IMMUTABLE,
        "ETag": etag,
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
    }
    # The URL's extension is cosmetic; never let the caller override MIME.
    media_type = hit.mime or "application/octet-stream"
    if hit.status == 304:
        return Response(status_code=304, headers=headers)
    if hit.status == 416:
        headers["Content-Range"] = f"bytes */{hit.size}"
        return Response(status_code=416, headers=headers)
    length = hit.size
    if hit.span is not None:
        start, end = hit.span
        headers["Content-Range"] = f"bytes {start}-{end}/{hit.size}"
        length = end - start + 1
    if head:
        headers["Content-Length"] = str(length)
        return Response(status_code=hit.status, headers=headers, media_type=media_type)
    return Response(
        content=hit.data, status_code=hit.status, headers=headers, media_type=media_type
    )


@router.get("/{slug}/_rt/{runtime_hash}/{path:path}", include_in_schema=False)
async def serve_runtime(slug: str, runtime_hash: str, path: str):
    if not _servable_slug(slug):
        raise HTTPException(404, "Not found.")
    segments = path.split("/")
    if (
        not path
        or "\\" in path
        or any(segment in ("", ".", "..") or segment.startswith(".") for segment in segments)
    ):
        raise HTTPException(404, "Not found.")
    runtime = RUNTIME
    data = runtime.files.get(path)
    if data is None:
        raise HTTPException(404, "Not found.")
    headers = {
        "Cache-Control": IMMUTABLE if runtime_hash == runtime.hash else NO_CACHE,
        "ETag": runtime.etags[path],
        "X-Content-Type-Options": "nosniff",
    }
    media_type = mime_for_ext(ext_of(path)) or "application/octet-stream"
    return Response(content=data, headers=headers, media_type=media_type)
