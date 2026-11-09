/* Showcase deck runtime — entry.
   Reads <script type="application/json" id="deck-data">, applies `when` flags, builds the
   .stage with one empty <section class="slide" data-slide=…> shell per visible slide, and
   hands the shells to the stage controller, which mounts content lazily (±2) and never
   unmounts. Layout slides are rendered once by Preact into their shell (the shell itself —
   its class and attributes — is never owned by Preact, so the controller can toggle classes
   freely); html slides are mounted imperatively. */

import { render } from "preact";
import { createStage } from "./stage.js";
import { LAYOUTS, TONES, DARK, Notes } from "./layouts.jsx";
import { mountHtmlSlide } from "./html-slide.js";
import { runDeckScript } from "./scripts.js";

const ID = /^[a-z0-9][a-z0-9-]{0,63}$/;

function readDeck() {
  const el = document.getElementById("deck-data");
  if (!el) return null;
  try {
    return JSON.parse(el.textContent || "null");
  } catch (err) {
    console.error("[deck] deck-data is not valid JSON", err);
    return null;
  }
}

function shown(when, flags) {
  if (when == null || when === "") return true;
  const w = String(when).trim();
  const neg = w.charAt(0) === "!";
  const on = flags[neg ? w.slice(1).trim() : w] === true;
  return neg ? !on : on;
}

function plain(text) {
  return String(text || "").replace(/\*\*/g, "").replace(/\s*\n\s*/g, " ").trim();
}

/* shrink [data-fit] figures until their content fits the box (stage px, transform-free) */
function fitText(scope) {
  scope.querySelectorAll("[data-fit]").forEach((box) => {
    const inner = box.firstElementChild;
    if (!inner) return;
    box.style.fontSize = "";
    const avail = box.clientWidth;
    if (!avail) return;
    let size = parseFloat(getComputedStyle(box).fontSize);
    for (let k = 0; k < 24 && inner.offsetWidth > avail && size > 24; k++) {
      size = Math.floor(size * 0.94);
      box.style.fontSize = size + "px";
    }
  });
}

function boot() {
  const deck = readDeck();
  const root = document.documentElement;
  if (!deck || !Array.isArray(deck.slides)) {
    root.classList.remove("is-pending");
    return;
  }
  const flags = deck.flags && typeof deck.flags === "object" ? deck.flags : {};
  const slides = deck.slides.filter((s) => s && typeof s === "object" && shown(s.when, flags));
  if (deck.title && !document.title) document.title = deck.title;
  root.classList.add(deck.theme === "none" ? "theme-none" : "theme-neutral");

  /* the wrapper's <div id="deck-root"> becomes the .stage */
  let stage = document.querySelector(".stage") || document.getElementById("deck-root");
  if (!stage) {
    stage = document.createElement("div");
    document.body.insertBefore(stage, document.body.firstChild);
  }
  stage.classList.add("stage");

  let seenMain = false;
  const items = slides.map((slide, i) => {
    const el = document.createElement("section");
    el.className = "slide";
    el.setAttribute("data-slide", ID.test(slide.id || "") ? slide.id : "slide-" + (i + 1));
    /* a backup needs a main slide above it; a leading "backup" is a main slide */
    const backup = slide.backup === true && seenMain;
    if (!backup) seenMain = true;
    if (backup) el.setAttribute("data-backup", "");
    el.setAttribute("tabindex", "-1");
    el.setAttribute("aria-roledescription", "slide");
    el.setAttribute("aria-label", i + 1 + " of " + slides.length);
    el.setAttribute("aria-hidden", "true");
    stage.appendChild(el);
    return { el, slide, backup, mounted: false };
  });

  function mountLayout(it) {
    const s = it.slide;
    const L = LAYOUTS[s.layout];
    const el = it.el;
    if (!L) {
      el.classList.add("layout-unknown", "on-paper");
      render(<p class="lt-unknown">Unknown layout “{String(s.layout)}”</p>, el);
      return;
    }
    /* full-bleed media always carries light type on a dark scrim: a light tone falls back to dark */
    let tone = TONES[s.tone] ? s.tone : L.tone(s);
    const bleed = L.bleed ? L.bleed(s) : false;
    if (bleed && !DARK[tone]) tone = "dark";
    el.classList.add("lt-slide", "layout-" + s.layout, TONES[tone], DARK[tone] ? "is-dark" : "is-light");
    if (bleed) el.classList.add("has-bleed");
    const label = s.title || s.quote || s.caption;
    if (label) el.setAttribute("aria-label", plain(label));
    const fine = s.fine === false ? null : typeof s.fine === "string" ? s.fine : deck.fine || null;
    const C = L.C;
    render(
      <>
        <C s={s} fine={fine} />
        <Notes notes={s.notes} />
      </>,
      el,
    );
    fitText(el);
  }

  function mount(i) {
    const it = items[i];
    if (it.slide.layout === "html") mountHtmlSlide(it.el, it.slide.source);
    else mountLayout(it);
  }

  function runDeckScripts() {
    if (Array.isArray(deck.scriptSources)) {
      deck.scriptSources.forEach((code, k) => {
        const label = (Array.isArray(deck.scripts) && deck.scripts[k]) || "deck-script-" + (k + 1) + ".js";
        if (typeof code === "string") runDeckScript(code, null, label);
      });
    } else if (Array.isArray(deck.scripts)) {
      deck.scripts.forEach((url) => { if (typeof url === "string") runDeckScript(null, url, url); });
    }
  }

  const ctl = createStage({ items, mount, onFirstMount: runDeckScripts });
  ctl.start();

  /* refit once the real faces are in (first mounts can measure a fallback face) */
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => items.forEach((it) => { if (it.mounted) fitText(it.el); }));
  }
}

boot();
