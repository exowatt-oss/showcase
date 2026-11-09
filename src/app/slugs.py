"""Slug derivation and validation for hosted presentations."""

import re

# Path segments the app itself owns — never allowed as presentation slugs.
RESERVED_SLUGS = {
    "api",
    "docs",
    "redoc",
    "openapi.json",
    "static",
    "favicon.ico",
    "health",
    "healthz",
    "mcp",
}

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_title(html: str) -> str:
    """Return the document's <title> text, or '' if there isn't one."""
    m = _TITLE_RE.search(html)
    if not m:
        return ""
    title = _TAG_RE.sub("", m.group(1))
    return " ".join(title.split()).strip()[:300]


def slugify(text: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with '-', trim to 64 chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64].strip("-")
    return slug


def validate_slug(slug: str) -> str | None:
    """Return an error message if the slug is unusable, else None."""
    if not SLUG_RE.match(slug):
        return (
            "Slug must be 1-64 characters: lowercase letters, digits, and hyphens, "
            "starting with a letter or digit."
        )
    if slug in RESERVED_SLUGS:
        return f"'{slug}' is reserved by the app itself — pick another slug."
    return None
