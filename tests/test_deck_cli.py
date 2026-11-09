"""Tests for scripts/showcase-deck (CLI + legacy importer). No network, no server."""

from __future__ import annotations

import base64
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import stat
import sys
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "showcase-deck"


def _load():
    loader = importlib.machinery.SourceFileLoader("showcase_deck", str(SCRIPT))
    spec = importlib.util.spec_from_loader("showcase_deck", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["showcase_deck"] = module
    loader.exec_module(module)
    return module


sd = _load()

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(range(40))
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(60, 120))
JPG_OTHER = b"\xff\xd8\xff\xdb" + b"OOOOOOOO" + bytes(range(200, 250))
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00\x00\x02\x00" + bytes(range(90))
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>'


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def percent_encode_a_letter(payload: str) -> str:
    """What the legacy builder does: percent-encode some letters inside base64."""
    for i, ch in enumerate(payload):
        if ch.isalpha():
            return payload[:i] + f"%{ord(ch):02X}" + payload[i + 1 :]
    raise AssertionError("payload has no letter")


def legacy_deck() -> str:
    jpg_other = percent_encode_a_letter(b64(JPG_OTHER))
    assert "%" in jpg_other
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Legacy &amp; Test</title>
<link rel="icon" href="data:image/png;base64,{b64(PNG)}">
<style>body {{ margin: 0 }} .slide {{ background: url(data:image/png;base64,{b64(PNG)}); }}</style>
<style>.slide[data-slide="intro"] .head-rule {{ color: red }}</style>
<script defer src="https://umami.example/loader.js"></script>
<script defer src="https://umami.example/loader.js"></script>
</head>
<body>
<style id="shared">:where(.slide[data-section="s"]) h2 {{ font-size: 10px }}</style>
<section class="slide on-paper" data-slide="intro" data-section="s">
  <h2>Hi</h2>
  <img src="data:image/png;base64,{b64(PNG)}"
       srcset="data:image/png;base64,{b64(PNG)} 1x, data:image/jpeg;base64,{b64(JPG)} 2x">
  <div style="background-image:url('data:image/jpeg;base64,{jpg_other}')"></div>
  <section class="inner">nested section</section>
  <!-- <section class="slide" data-slide="commented"></section> -->
  <style>.slide[data-slide="intro"] h2 {{ color: blue }}</style>
</section>
<script>
(function () {{
  var sec = document.querySelector('.slide[data-slide="intro"]');
  var tpl = '<section class="slide" data-slide="fake">not a slide</section>';
}})();
</script>
<section data-backup="1" class="slide" data-slide="clip">
  <video src="data:video/mp4;base64,{b64(MP4)}" loop muted></video>
  <script>document.querySelector('.slide[data-slide="clip"] video').muted = true;</script>
</section>
<section class="slide" data-slide="clip" data-backup="1">
  <video src="data:video/mp4;base64,{b64(MP4)}" loop muted></video>
  <script>document.querySelector('.slide[data-slide="clip"] video').muted = true;</script>
</section>
<style>.slide[data-slide="clip"] video {{ width: 100% }}</style>
<script>/* SLIDE · stage.js v1
   window.ShowcaseStage = {{ go(i), next(), prev(), index(), count() }} */
(function () {{ var s = '<section class="slide">'; window.ShowcaseStage = {{}}; }})();
</script>
<script>console.log("global");</script>
<script>var icon = "data:image/svg+xml;base64,{b64(SVG)}";</script>
</body></html>
"""


# --------------------------------------------------------------------------- importer


@pytest.fixture
def imported():
    return sd.import_legacy(legacy_deck(), "synthetic")


def test_import_slides_ids_backups_and_title(imported):
    deck = imported["deck"]
    assert deck["schema"] == 1
    assert deck["title"] == "Legacy & Test"
    assert deck["theme"] == "none"
    assert deck["css"] == ["theme/deck.css"]
    assert [s["id"] for s in deck["slides"]] == ["intro", "clip", "clip-2"]
    assert [s.get("backup", False) for s in deck["slides"]] == [False, True, True]
    assert all(s["layout"] == "html" for s in deck["slides"])
    assert deck["slides"][0]["html"] == "slides/01-intro.html"
    assert deck["slides"][2]["html"] == "slides/03-clip-2.html"
    assert imported["summary"]["renamed_ids"] == {"clip": ["clip", "clip-2"]}


def test_import_ignores_section_text_in_scripts_and_comments(imported):
    intro = imported["texts"]["slides/01-intro.html"]
    assert len(sd.find_slide_sections(intro)) == 1
    assert "nested section" in intro  # nested <section> stays inside its slide
    assert 'data-slide="fake"' in intro  # the script string came along, untouched


def test_import_assets_extracted_deduplicated_and_verified(imported):
    assets = imported["assets"]
    by_content = {hashlib.sha256(data).hexdigest(): (path, data) for path, data in assets.items()}
    expected = {PNG: "png", JPG: "jpg", JPG_OTHER: "jpg", MP4: "mp4", SVG: "svg"}
    assert len(assets) == len(expected)  # PNG/MP4 repeats stored once; favicon dropped
    for data, ext in expected.items():
        sha = hashlib.sha256(data).hexdigest()
        path, stored = by_content[sha]
        assert stored == data  # percent-encoded payload decoded to the exact bytes
        assert path == f"assets/{sha[:12]}.{ext}"
    summary = imported["summary"]
    assert summary["unique_assets"] == 5
    assert summary["duplicates_removed"] == summary["asset_references"] - 5 > 0
    for path, text in imported["texts"].items():
        assert "data:" not in text, path


def test_import_rewrites_references_everywhere(imported):
    texts = imported["texts"]
    png = f"assets/{hashlib.sha256(PNG).hexdigest()[:12]}.png"
    jpg = f"assets/{hashlib.sha256(JPG).hexdigest()[:12]}.jpg"
    other = f"assets/{hashlib.sha256(JPG_OTHER).hexdigest()[:12]}.jpg"
    svg = f"assets/{hashlib.sha256(SVG).hexdigest()[:12]}.svg"
    intro = texts["slides/01-intro.html"]
    assert f'src="{png}"' in intro
    assert f'srcset="{png} 1x, {jpg} 2x"' in intro
    assert f"url('{other}')" in intro
    assert f"url({png})" in texts["theme/deck.css"]
    assert any(svg in text for path, text in texts.items() if path.endswith(".js"))


def test_import_css_js_placement(imported):
    deck, texts = imported["deck"], imported["texts"]
    css = texts["theme/deck.css"]
    assert "margin: 0" in css  # head style
    assert "head-rule" in css  # head styles stay deck-level even when slide-scoped
    assert "data-section" in css  # cross-slide body style stays deck-level
    assert "ShowcaseStage" not in "".join(texts.values())  # stage.js dropped
    assert imported["summary"]["stage_js_dropped"] == 1
    scripts = [texts[p] for p in deck["scripts"]]
    assert len(scripts) == 3  # umami loader (deduplicated), console.log, svg icon
    assert "umami.example/loader.js" in scripts[0]
    assert 'console.log("global")' in scripts[1]
    intro = texts["slides/01-intro.html"]
    # the script between intro and clip that targets intro belongs to intro
    assert "document.querySelector('.slide[data-slide=\"intro\"]')" in intro
    assert intro.index("</section>\n<script>") > 0


def test_import_duplicate_ids_keep_working(imported):
    texts = imported["texts"]
    first, second = texts["slides/02-clip.html"], texts["slides/03-clip-2.html"]
    assert sd.find_slide_sections(second)[0][2].attrs()["data-slide"] == "clip-2"
    # the duplicate's own CSS/JS is retargeted at itself
    assert '.slide[data-slide="clip-2"] video { width: 100% }' in second
    assert "querySelector('.slide[data-slide=\"clip-2\"] video')" in second
    assert 'data-slide="clip"' not in second.replace('data-slide="clip-2"', "")
    # the first copy's CSS covers both copies without changing specificity
    assert ':is([data-slide="clip"],[data-slide="clip-2"]) video' in first
    # ... while its JS still targets the first copy, as in the original
    assert 'querySelector(\'.slide:is([data-slide="clip"],[data-slide="clip-2"]) video\')' in (
        first
    )  # first match in document order is still the first copy, as in the original


def test_import_writes_a_valid_bundle(tmp_path, capsys):
    src = tmp_path / "legacy.html"
    src.write_text(legacy_deck(), encoding="utf-8")
    out = tmp_path / "bundle"
    assert sd.main(["import", str(src), str(out)]) == 0
    printed = capsys.readouterr().out
    assert "3 (2 backup)" in printed and "5 unique" in printed
    report, deck, files = sd.validate_bundle(out)
    assert report.ok, report.errors
    assert "deck.json" in files and len(deck["slides"]) == 3
    assert sd.main(["import", str(src), str(out)]) == 1  # refuses a non-empty folder
    assert sd.main(["import", str(src), str(out), "--force"]) == 0


def test_import_requires_slide_sections():
    with pytest.raises(sd.CliError):
        sd.import_legacy("<html><body><div>nothing</div></body></html>")


def test_sniff_corrects_a_wrong_mime():
    store = sd.AssetStore()
    path = store.add("image/jpeg", PNG)
    assert path.endswith(".png")
    assert store.notes


def test_css_single_owner():
    known = {"a", "b"}
    assert sd.css_single_owner('.slide[data-slide="a"] x{} @keyframes k{from{}to{}}', known) == "a"
    assert sd.css_single_owner('.slide[data-slide="a"] x{} .y{}', known) is None
    assert sd.css_single_owner('[data-slide="a"] x, [data-slide="b"] y {}', known) is None


def cascade_deck() -> str:
    """The aws-five-v7 shape: a slide's own <style> (inside its section) followed, later in
    the document, by a multi-slide <style> with an equally specific rule for the same
    property. In the source the later rule wins; the bundle must keep it that way."""
    return """<!doctype html><html><head><title>Cascade</title>
<style>.slide { color: black }</style>
</head><body>
<section class="slide" data-slide="op" data-section="story">
  <p class="num">47 <em>to</em> 98</p>
  <style>.slide[data-slide="op"] .done .num { letter-spacing: -.03em; }
  .slide[data-slide="op"] .keep { color: red; }</style>
</section>
<section class="slide" data-slide="rnd" data-section="story">
  <p class="num">1</p>
  <style>.slide[data-slide="rnd"] .solo { width: 1px }</style>
</section>
<section class="slide" data-slide="plain"><p class="x">x</p></section>
<style id="plain-style">.slide[data-slide="plain"] .x { margin: 0 }</style>
<style id="story-style">
.slide[data-section="story"]:is([data-slide="rnd"],[data-slide="op"]) .num {
  letter-spacing: -.05em; }
.slide[data-section="story"] .dead-class { width: 2px }
</style>
</body></html>
"""


def test_import_keeps_cascade_order_of_equal_specificity_rules():
    result = sd.import_legacy(cascade_deck(), "cascade")
    texts = result["texts"]
    css, op = texts["theme/deck.css"], texts["slides/01-op.html"]
    # the op slide's in-section style moves into deck.css *before* the story style ...
    assert css.index("letter-spacing: -.03em") < css.index("letter-spacing: -.05em")
    assert "from slide op" in css
    # ... and leaves an empty <style> behind, so the slide's DOM is unchanged
    assert "letter-spacing" not in op
    assert "<style></style>" in op
    # rules that cannot tie with a later one stay with their slide
    assert ".solo" in texts["slides/02-rnd.html"] and ".solo" not in css
    assert "plain-style" not in css and ".x { margin: 0 }" in texts["slides/03-plain.html"]


def test_plan_style_placement_reasons():
    def blocks(html):
        out = []
        for tag in sd.iter_tags(html):
            if tag.name == "style" and not tag.closing:
                out.append(sd.Block(tag, html, 0, False))
        return out

    known = {"a", "b"}
    # a slide-owned top-level style before a deck-level rule that can tie -> deck
    html = (
        '<style>.slide[data-slide="a"] .t { color: red }</style>'
        '<style>.slide[data-slide="a"] .t, .slide[data-slide="b"] .t { color: blue }</style>'
    )
    first, second = blocks(html)
    plan = sd.plan_style_placement([first, second], known, sd.MarkupClasses('<p class="t"></p>'))
    assert plan == {first.position: "deck", second.position: "deck"}
    # higher specificity later, other property, other slide, or a class no element has: safe
    for later in (
        '.slide[data-slide="a"] p.t { color: blue }',  # (0,3,1) beats (0,3,0) anyway
        '.slide[data-slide="a"] .t, .slide[data-slide="b"] .t { width: 1px }',
        '.slide[data-slide="b"] .t, .slide[data-slide="b"] .u { color: blue }',
        '.slide[data-slide="a"] .gone, .slide[data-slide="b"] .gone { color: blue }',
    ):
        first, second = blocks(
            f'<style>.slide[data-slide="a"] .t {{ color: red }}</style><style>{later}</style>'
        )
        plan = sd.plan_style_placement(
            [first, second], known, sd.MarkupClasses('<p class="t"></p>')
        )
        assert plan[first.position] == "slide", later


def test_specificity_and_subjects():
    spec = sd.selector_specificity
    assert spec('.slide[data-slide="x"] .a .b') == (0, 4, 0)
    assert spec('.slide[data-section="s"]:is([data-slide="x"],[data-slide="y"]) .b') == (0, 4, 0)
    assert spec(":where(.slide[data-section='s']) .b") == (0, 1, 0)
    assert spec("#id > p::before") == (1, 0, 2)
    assert spec("li:nth-child(2n of .on):not(.x, #y)") == (1, 2, 1)
    assert sd.selector_subject('.slide[data-slide="a"] .x:not(:is(.y)).w > p.q') == (
        "p",
        frozenset({"q"}),
    )
    assert sd.property_root("margin-left") == sd.property_root("margin")
    assert sd.property_root("line-height") == sd.property_root("font")
    assert sd.property_root("--d") != sd.property_root("--dur")
    rules = sd.css_rules(
        "@media (min-width: 1px) { .a { color: red !important } } @keyframes k { to { x: 1 } }"
    )
    assert ([".a"], {("color", True)}) in rules
    assert ([""], {("@keyframes k", False)}) in rules
    markup = sd.MarkupClasses('<p class="a b"></p><script>el.className = "js-" + k;</script>')
    assert markup.can_share(("", frozenset({"a"})), ("", frozenset({"b"})))
    assert not markup.can_share(("p", frozenset()), ("div", frozenset()))
    assert not markup.can_share(("", frozenset({"a"})), ("", frozenset({"never"})))
    assert markup.can_share(("", frozenset({"a"})), ("", frozenset({"js-made"})))  # script-built


# --------------------------------------------------------------------------- validate


def write_bundle(root: Path, deck: dict, files: dict[str, bytes | str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "deck.json").write_text(json.dumps(deck), encoding="utf-8")
    for path, data in (files or {}).items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            dest.write_text(data, encoding="utf-8")
        else:
            dest.write_bytes(data)
    return root


GOOD_FILES = {
    "assets/bg.jpg": JPG,
    "assets/clip.mp4": MP4,
    "slides/lab.html": '<section class="slide" data-slide="lab"><img src="assets/bg.jpg"></section>'
    '\n<style>.slide[data-slide="lab"] img{width:10px}</style>',
    "theme/extra.css": ".x { background: url(assets/bg.jpg) }",
    "theme/extra.js": "console.log(1)",
}


def good_deck() -> dict:
    return {
        "schema": 1,
        "title": "Good deck",
        "theme": "neutral",
        "css": ["theme/extra.css"],
        "scripts": ["theme/extra.js"],
        "flags": {"openSource": True},
        "fine": "Synthetic example.",
        "slides": [
            {"id": "opener", "layout": "title", "title": "Hello", "media": "assets/bg.jpg"},
            {
                "id": "proof",
                "layout": "stats",
                "title": "Numbers",
                "stats": [{"value": "1", "label": "one"}],
                "notes": "n",
                "tone": "dark",
            },
            {"id": "lab", "layout": "html", "html": "slides/lab.html"},
            {
                "id": "clip",
                "layout": "hero-media",
                "title": "Clip",
                "media": {"src": "assets/clip.mp4", "poster": "assets/bg.jpg", "fit": "cover"},
                "side": "left",
            },
            {
                "id": "steps",
                "layout": "steps",
                "title": "S",
                "steps": [{"title": "a"}, {"title": "b"}],
            },
            {"id": "pics", "layout": "gallery", "items": [{"media": "assets/bg.jpg"}]},
            {"id": "oss", "layout": "statement", "title": "Open", "when": "openSource"},
            {
                "id": "bye",
                "layout": "close",
                "title": "Bye",
                "points": [],
                "fine": False,
                "backup": True,
            },
        ],
    }


def test_validate_good_deck(tmp_path, capsys):
    root = write_bundle(tmp_path / "deck", good_deck(), GOOD_FILES)
    (root / ".preview").mkdir()
    (root / ".preview" / "index.html").write_text("ignored")
    (root / ".DS_Store").write_bytes(b"ignored")
    report, deck, files = sd.validate_bundle(root)
    assert report.ok, report.errors
    assert report.warnings == []
    assert ".DS_Store" not in files and not any(p.startswith(".preview") for p in files)
    assert sd.main(["validate", str(root)]) == 0
    assert "OK, 8 slides" in capsys.readouterr().out


def _errors_for(tmp_path, mutate, files=None) -> list[str]:
    deck = good_deck()
    mutate(deck)
    root = write_bundle(tmp_path / "deck", deck, files if files is not None else GOOD_FILES)
    report, _, _ = sd.validate_bundle(root)
    return report.errors


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda d: d.update(schema=2), "schema"),
        (lambda d: d.pop("title"), "title"),
        (lambda d: d.update(theme="dark"), "theme"),
        (lambda d: d.update(slides=[]), "slides"),
        (lambda d: d["slides"][1].update(id="Proof!"), "slides[1].id"),
        (lambda d: d["slides"][1].update(id="opener"), "duplicate id"),
        (lambda d: d["slides"][0].update(layout="hero"), "layout"),
        (lambda d: d["slides"][0].pop("title"), "slide 'opener' (#1).title: is required"),
        (lambda d: d["slides"][1].update(stats=[]), "stats"),
        (lambda d: d["slides"][1]["stats"][0].pop("label"), "stats[0].label"),
        (lambda d: d["slides"][3].pop("media"), "slide 'clip' (#4).media: is required"),
        (lambda d: d["slides"][3]["media"].update(fit="fill"), "media.fit"),
        (lambda d: d["slides"][3].update(side="up"), "side"),
        (lambda d: d["slides"][4].update(steps=[{"title": "a"}]), "steps"),
        (lambda d: d["slides"][0].update(media="assets/missing.jpg"), "not a file in the bundle"),
        (lambda d: d["slides"][0].update(media="../secret.jpg"), "'..'"),
        (lambda d: d["slides"][0].update(media="/abs/x.jpg"), "slide 'opener'"),
        (lambda d: d["slides"][2].update(tone="dark"), "tone is for layout slides only"),
        (lambda d: d["slides"][1].update(tone="neon"), "tone"),
        (lambda d: d["slides"][2].update(html="slides/nope.html"), "not a file"),
        (lambda d: d["slides"][6].update(when=5), "when"),
        (lambda d: d["slides"][7].update(points=["a", "b", "c", "d", "e"]), "points"),
        (lambda d: d["slides"][7].update(fine=True), "fine"),
        (lambda d: d.update(flags={"x": "yes"}), "flags.x"),
        (lambda d: d.update(css=["theme/nope.css"]), "css[0]"),
    ],
)
def test_validate_rejects(tmp_path, mutate, needle):
    errors = _errors_for(tmp_path, mutate)
    assert errors, "expected a validation error"
    assert any(needle in e for e in errors), errors


def test_validate_html_slide_needs_one_section(tmp_path):
    files = dict(GOOD_FILES)
    files["slides/lab.html"] = "<div>no section</div>"
    errors = _errors_for(tmp_path, lambda d: None, files)
    assert any("exactly one" in e for e in errors)


def test_validate_bad_file_names_and_size(tmp_path, monkeypatch):
    files = dict(GOOD_FILES)
    files["assets/bad name.png"] = PNG
    errors = _errors_for(tmp_path, lambda d: None, files)
    assert any("bad name.png" in e for e in errors)
    monkeypatch.setattr(sd, "MAX_FILE_BYTES", 50)
    errors = _errors_for(tmp_path / "x", lambda d: None)
    assert any("64 MiB" in e for e in errors)


def test_validate_warnings(tmp_path):
    deck = good_deck()
    deck["slides"][6]["when"] = "!undeclared"
    files = dict(GOOD_FILES)
    files["theme/extra.css"] = ".x { background: url(assets/gone.png) }"
    root = write_bundle(tmp_path / "deck", deck, files)
    report, _, _ = sd.validate_bundle(root)
    assert report.ok
    assert any("undeclared" in w for w in report.warnings)
    assert any("assets/gone.png" in w for w in report.warnings)


def test_validate_bad_json(tmp_path):
    root = tmp_path / "deck"
    root.mkdir()
    (root / "deck.json").write_text("{nope")
    assert sd.main(["validate", str(root)]) == 1


# --------------------------------------------------------------------------- asset references


def test_rewrite_refs_boundaries():
    mapping = {"assets/a.png": "/s/_a/A.png", "assets/a.png.bak": "/s/_a/B.bak"}
    text = (
        'src="assets/a.png" url(assets/a.png) x/assets/a.png xassets/a.png '
        "./assets/a.png assets/a.png.bak assets/other.png"
    )
    out = sd.rewrite_refs(text, mapping)
    assert out == (
        'src="/s/_a/A.png" url(/s/_a/A.png) x/assets/a.png xassets/a.png '
        "./assets/a.png /s/_a/B.bak assets/other.png"
    )


def test_rewrite_deck_value_skips_non_reference_keys():
    mapping = {"assets/x.jpg": "../assets/x.jpg", "left": "WRONG"}
    slide = {
        "id": "left",
        "side": "left",
        "media": {"src": "assets/x.jpg", "fit": "cover"},
        "title": "see assets/x.jpg",
    }
    out = sd.rewrite_deck_value(slide, mapping)
    assert out["id"] == "left" and out["side"] == "left"
    assert out["media"]["src"] == "../assets/x.jpg"
    assert out["title"] == "see ../assets/x.jpg"


# --------------------------------------------------------------------------- preview


def test_preview_json_shape(tmp_path, capsys):
    root = write_bundle(tmp_path / "deck", good_deck(), GOOD_FILES)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in ("runtime.js", "stage.css", "theme-neutral.css"):
        (runtime / name).write_text(f"/* {name} */")
    assert sd.main(["preview", str(root), "--runtime", str(runtime)]) == 0
    assert "http.server" in capsys.readouterr().out
    page = (root / ".preview" / "index.html").read_text()
    assert (root / ".preview" / "_rt" / "runtime.js").is_file()
    assert '<link rel="stylesheet" href="_rt/stage.css">' in page
    assert '<link rel="stylesheet" href="_rt/theme-neutral.css">' in page
    assert '<script src="_rt/runtime.js" defer></script>' in page
    assert '<div id="deck-root"></div>' in page
    assert "url(../assets/bg.jpg)" in page  # deck CSS inlined with relative refs
    blob = re.search(r'<script type="application/json" id="deck-data">(.*?)</script>', page, re.S)
    raw = blob.group(1)
    assert "<" not in raw and "\\u003c" in raw
    data = json.loads(raw)
    assert list(data) == [
        "slug", "version", "title", "theme", "flags", "fine", "slides", "scriptSources",
    ]  # fmt: skip
    assert data["slug"] == "deck" and data["theme"] == "neutral"
    assert data["flags"] == {"openSource": True} and data["fine"] == "Synthetic example."
    assert data["slides"][0]["media"] == "../assets/bg.jpg"
    lab = data["slides"][2]
    assert lab["html"] == "../slides/lab.html"
    assert lab["source"].startswith('<section class="slide" data-slide="lab"><img src="../assets')
    assert data["slides"][3]["media"]["fit"] == "cover"
    assert data["scriptSources"] == ["console.log(1)"]
    # a second preview replaces the first, and .preview is never part of the bundle
    assert sd.main(["preview", str(root), "--runtime", str(runtime)]) == 0
    assert not any(p.startswith(".preview") for p in sd.scan_bundle(root)[0])


def test_preview_warns_when_runtime_missing(tmp_path, capsys):
    root = write_bundle(tmp_path / "deck", good_deck(), GOOD_FILES)
    assert sd.main(["preview", str(root), "--runtime", str(tmp_path / "none")]) == 0
    assert "runtime" in capsys.readouterr().err
    assert (root / ".preview" / "index.html").is_file()


# --------------------------------------------------------------------------- push / token


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeServer:
    """Stands in for urlopen: a tiny in-memory Showcase deck API."""

    def __init__(self, stored=(), fail_once=()):
        self.stored: dict[str, bytes] = {hashlib.sha256(d).hexdigest(): d for d in stored}
        self.fail_once = set(fail_once)
        self.calls: list[tuple[str, str, dict]] = []
        self.versions: list[dict] = []

    def __call__(self, req, timeout=None):
        method, url = req.get_method(), req.full_url
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append((method, "/" + path, dict(req.header_items())))
        if method in self.fail_once:
            self.fail_once.discard(method)
            raise urllib.error.HTTPError(url, 503, "busy", {}, io.BytesIO(b"{}"))
        body = req.data
        if path == "api/assets/missing":
            hashes = json.loads(body)["hashes"]
            missing = [h for h in hashes if h not in self.stored]
            return FakeResponse(200, json.dumps({"missing": missing}).encode())
        if path.startswith("api/assets/"):
            sha = path.rsplit("/", 1)[1]
            assert hashlib.sha256(body).hexdigest() == sha
            self.stored[sha] = body
            return FakeResponse(201, b"{}")
        m = re.match(r"api/decks/([a-z0-9-]+)/versions$", path)
        if m and method == "POST":
            payload = json.loads(body)
            version = len(self.versions) + 1
            self.versions.append({"version": version, **payload})
            slug = m.group(1)
            reply = {
                "slug": slug,
                "version": version,
                "url": f"/{slug}",
                "pinned_url": f"/{slug}/v{version}",
                "size_bytes": 1,
            }
            return FakeResponse(201, json.dumps(reply).encode())
        if m and method == "GET":
            rows = [
                {
                    "version": v["version"],
                    "message": v["message"],
                    "size_bytes": 1,
                    "created_at": "2026-11-06T12:00:00+00:00",
                }
                for v in reversed(self.versions)
            ]
            return FakeResponse(200, json.dumps(rows).encode())
        m = re.match(r"api/decks/([a-z0-9-]+)/versions/(\d+)$", path)
        if m:
            v = self.versions[int(m.group(2)) - 1]
            return FakeResponse(
                200,
                json.dumps(
                    {
                        "version": v["version"],
                        "deck": v["deck"],
                        "files": v["files"],
                        "message": v["message"],
                        "created_at": None,
                    }
                ).encode(),
            )
        m = re.match(r"([a-z0-9-]+)/_a/([0-9a-f]{64})\.\w+$", path)
        if m:
            return FakeResponse(200, self.stored[m.group(2)])
        raise AssertionError(f"unexpected request {method} {path}")


@pytest.fixture
def fake_server(monkeypatch):
    server = FakeServer(stored=[MP4], fail_once={"PUT"})
    real_client = sd.Client

    def client(base, token, **kw):
        return real_client(base, token, sleep=lambda s: None, opener=server)

    monkeypatch.setattr(sd, "Client", client)
    monkeypatch.setenv("SHOWCASE_URL", "https://showcase.test/")
    monkeypatch.setenv("SHOWCASE_OWNER_TOKEN", "sekret-token-value")
    return server


def test_push_uploads_only_missing_and_publishes(tmp_path, fake_server, capsys):
    root = write_bundle(tmp_path / "deck", good_deck(), GOOD_FILES)
    assert sd.main(["push", str(root), "--slug", "my-deck", "-m", "first"]) == 0
    out = capsys.readouterr()
    assert "sekret-token-value" not in out.out + out.err
    assert "https://showcase.test/my-deck" in out.out
    assert "https://showcase.test/my-deck/v1" in out.out
    assert "uploaded" in out.out and "reused" in out.out
    puts = [c for c in fake_server.calls if c[0] == "PUT"]
    put_hashes = {c[1].rsplit("/", 1)[1] for c in puts}
    assert hashlib.sha256(MP4).hexdigest() not in put_hashes  # already stored: reused
    assert "retrying" in out.err  # the first PUT got a 503 and was retried
    assert hashlib.sha256(JPG).hexdigest() in put_hashes
    assert len(puts) == len(GOOD_FILES) + 1  # every file incl. deck.json, mp4 skipped, 1 retry
    content_types = {c[1].rsplit("/", 1)[1]: c[2].get("Content-type") for c in puts}
    assert content_types[hashlib.sha256(JPG).hexdigest()] == "image/jpeg"
    pushed = fake_server.versions[0]
    assert pushed["message"] == "first"
    assert set(pushed["files"]) == set(GOOD_FILES) | {"deck.json"}
    assert all(c[2].get("X-owner-token") == "sekret-token-value" for c in fake_server.calls)
    post = [c for c in fake_server.calls if c[1] == "/api/decks/my-deck/versions"][0]
    assert post[2].get("X-idempotency-key", "").startswith("showcase-deck-")


def test_push_refuses_invalid_bundle(tmp_path, fake_server, capsys):
    deck = good_deck()
    deck["slides"][0]["media"] = "assets/missing.jpg"
    root = write_bundle(tmp_path / "deck", deck, GOOD_FILES)
    assert sd.main(["push", str(root), "--slug", "my-deck"]) == 1
    assert fake_server.calls == []


def test_versions_pull_and_diff(tmp_path, fake_server, capsys):
    root = write_bundle(tmp_path / "deck", good_deck(), GOOD_FILES)
    assert sd.main(["push", str(root), "--slug", "my-deck", "-m", "one"]) == 0
    deck = good_deck()
    deck["title"] = "Better deck"
    write_bundle(root, deck, {"assets/new.png": PNG})
    (root / "theme" / "extra.js").unlink()
    deck["scripts"] = []
    write_bundle(root, deck)
    assert sd.main(["push", str(root), "--slug", "my-deck", "-m", "two"]) == 0
    capsys.readouterr()

    assert sd.main(["versions", "my-deck"]) == 0
    listing = capsys.readouterr().out
    assert listing.index("v2") < listing.index("v1") and "two" in listing

    out = tmp_path / "pulled"
    assert sd.main(["pull", "my-deck", str(out), "--version", "1"]) == 0
    assert (out / "assets" / "clip.mp4").read_bytes() == MP4
    assert json.loads((out / "deck.json").read_text())["title"] == "Good deck"
    report, _, _ = sd.validate_bundle(out)
    assert report.ok
    assert sd.main(["pull", "my-deck", str(out)]) == 1  # not empty

    capsys.readouterr()
    assert sd.main(["diff", "my-deck", "1", "v2"]) == 0
    diff = capsys.readouterr().out
    assert '-  "title": "Good deck"' in diff and '+  "title": "Better deck"' in diff
    assert "added    assets/new.png" in diff
    assert "removed  theme/extra.js" in diff


def test_pull_rejects_tampered_bytes(tmp_path, fake_server):
    root = write_bundle(tmp_path / "deck", good_deck(), GOOD_FILES)
    assert sd.main(["push", str(root), "--slug", "my-deck"]) == 0
    for sha in fake_server.stored:
        fake_server.stored[sha] = b"tampered"
    assert sd.main(["pull", "my-deck", str(tmp_path / "out")]) == 1


def test_client_gives_up_after_retries_and_hides_token():
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        body = io.BytesIO(b'{"detail":"echo s3cr3t-T0KEN"}')
        raise urllib.error.HTTPError(req.full_url, 502, "bad", {}, body)

    client = sd.Client("https://x", "s3cr3t-T0KEN", retries=2, sleep=lambda s: None, opener=opener)
    with pytest.raises(sd.HttpError) as exc:
        client.request("GET", "/api/x")
    assert len(calls) == 3
    assert "s3cr3t-T0KEN" not in str(exc.value)


def test_client_does_not_retry_4xx():
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        detail = b'{"detail":"/x is a single-file HTML page"}'
        raise urllib.error.HTTPError(req.full_url, 409, "conflict", {}, io.BytesIO(detail))

    client = sd.Client("https://x", None, sleep=lambda s: None, opener=opener)
    with pytest.raises(sd.HttpError, match="409: /x is a single-file HTML page"):
        client.request("POST", "/api/decks/x/versions", body=b"{}")
    assert len(calls) == 1


def test_cli_refuses_non_loopback_http_and_disables_redirects(monkeypatch):
    monkeypatch.setenv("SHOWCASE_URL", "http://upload.example.org")
    with pytest.raises(sd.CliError, match="HTTPS"):
        sd.base_url()
    with pytest.raises(sd.CliError, match="HTTPS"):
        sd.Client("http://upload.example.org", "owner")
    monkeypatch.setenv("SHOWCASE_URL", "http://127.0.0.1:8000")
    assert sd.base_url() == "http://127.0.0.1:8000"
    assert (
        sd._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example.org")
        is None
    )
    captured = []

    def capture(req, timeout):
        captured.append(req)
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {}, io.BytesIO(b""))

    monkeypatch.setenv("SHOWCASE_AUTH_TOKEN", "oidc-example-token")
    client = sd.Client("http://127.0.0.1:8000", "owner-secret", retries=0, opener=capture)
    with pytest.raises(sd.HttpError) as err:
        client.request("GET", "/api/decks/x/versions")
    assert err.value.status == 302
    req = captured[0]
    assert "Authorization" in req.unredirected_hdrs
    assert "X-owner-token" in req.unredirected_hdrs
    assert "Authorization" not in req.headers
    assert "X-owner-token" not in req.headers


def test_token_file_created_private(tmp_path, monkeypatch):
    monkeypatch.delenv("SHOWCASE_OWNER_TOKEN", raising=False)
    token_file = tmp_path / "cfg" / "owner-token"
    monkeypatch.setenv("SHOWCASE_OWNER_TOKEN_FILE", str(token_file))
    assert sd.load_token(create=False) is None
    token = sd.load_token(create=True)
    assert token and token_file.read_text().strip() == token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    assert sd.load_token(create=True) == token  # idempotent
    assert os.listdir(token_file.parent) == ["owner-token"]  # no temp files left behind


def test_token_symlink_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("SHOWCASE_OWNER_TOKEN", raising=False)
    real = tmp_path / "real"
    real.write_text("abc\n")
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("SHOWCASE_OWNER_TOKEN_FILE", str(link))
    with pytest.raises(sd.CliError, match="symlink"):
        sd.load_token(create=False)


# --------------------------------------------------------------------------- init


def test_init_scaffold_validates(tmp_path, capsys):
    root = tmp_path / "q3-review"
    assert sd.main(["init", str(root)]) == 0
    deck = json.loads((root / "deck.json").read_text())
    assert deck["title"] == "Q3 Review"
    assert 3 <= len(deck["slides"]) <= 4
    report, _, _ = sd.validate_bundle(root)
    assert report.ok, report.errors
    assert sd.main(["init", str(root)]) == 1  # never overwrites


def test_preview_wrapper_matches_server_wrapper(tmp_path):
    """Drift guard: with server URLs plugged in, the preview page is the server's page."""
    decks = pytest.importorskip("app.decks")
    if not hasattr(decks, "render_wrapper"):
        pytest.skip("server wrapper not available")
    root = write_bundle(tmp_path / "deck", good_deck(), GOOD_FILES)
    deck = json.loads((root / "deck.json").read_text())
    files, _ = sd.scan_bundle(root)
    shas = {path: sd.sha256_file(full) for path, full in files.items()}
    texts_by_sha = {
        shas[p]: full.read_text()
        for p, full in files.items()
        if p.endswith((".html", ".css", ".js"))
    }
    server_page = decks.render_wrapper(
        slug="my-deck",
        version=3,
        deck=deck,
        files=shas,
        texts=texts_by_sha,
        runtime_hash="abc123",
    )
    mapping = {path: decks.asset_url("my-deck", sha, path) for path, sha in shas.items()}
    ours = sd.render_wrapper(
        deck,
        sd.bundle_texts(files, deck),
        mapping,
        slug="my-deck",
        version=3,
        runtime_base="/my-deck/_rt/abc123",
    )
    assert ours == server_page
