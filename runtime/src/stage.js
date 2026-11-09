/* Showcase deck runtime · stage controller.
   A standalone deck navigation runtime (keys, touch, click, #N deep links,
   ?print ?static ?grid, reduced motion, letterbox, counters, data-draw, folio, videos,
   showcase:slide, window.ShowcaseStage), plus:
     - backup slides: skipped by → / ← / space / click, reached with ↓ from the main slide
       above them, ↑ goes back up (a short vertical move instead of the crossfade);
     - lazy mounting: a slide's content is mounted when it comes within 2 of the current
       slide (or is one navigation step away), all of them in ?print, never unmounted.
   The controller only ever touches section classes/attributes imperatively; slide content is
   mounted once by `mount(i)` and never re-rendered. */

const W = 1920;
const H = 1080;
const pad = (n) => (n < 10 ? "0" : "") + n;

export function createStage(opts) {
  const { items, mount, onFirstMount } = opts;
  const root = document.documentElement;
  const params = new URLSearchParams(location.search);
  const reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const isPrint = params.has("print");
  const isStatic = params.has("static") || reduce || isPrint;
  if (isPrint) root.classList.add("is-print");
  if (isStatic) root.classList.add("no-motion");
  if (params.has("grid")) root.classList.add("show-grid");
  if (!isPrint) root.classList.add("is-pending");

  const n = items.length;
  const counters = new WeakMap();
  let current = -1;
  let firstMountDone = false;
  let vTimer = 0;
  const vMoved = [];

  /* ---- folio: main slides count among main slides; a backup reads like "07a" */
  const mains = items.filter((it) => !it.backup).length || n;
  const folios = [];
  {
    let m = 0;
    let sub = 0;
    items.forEach((it) => {
      if (!it.backup) {
        m += 1;
        sub = 0;
        folios.push("<b>" + pad(m) + "</b> / " + pad(mains));
      } else {
        sub += 1;
        const letter = String.fromCharCode(96 + Math.min(sub, 26));
        folios.push("<b>" + pad(Math.max(m, 1)) + letter + "</b> / " + pad(mains));
      }
    });
  }

  /* ---- mounting */
  function ensure(i) {
    const it = items[i];
    if (!it || it.mounted) return;
    it.mounted = true;
    mount(i);
    const s = it.el;
    s.querySelectorAll("[data-folio='auto']").forEach((el) => { el.innerHTML = folios[i]; });
    s.querySelectorAll("[data-draw]").forEach((el) => {
      if (!el.hasAttribute("pathLength")) el.setAttribute("pathLength", "1");
    });
    s.querySelectorAll("video").forEach(guardVideo);
  }
  function neighbours(i) {
    const set = new Set();
    for (let k = i - 2; k <= i + 2; k++) if (k >= 0 && k < n) set.add(k);
    [nextMain(i), prevMain(i), below(i), above(i)].forEach((k) => { if (k >= 0) set.add(k); });
    return set;
  }
  function mountAround(i) {
    ensure(i);                                   /* the target first, then its neighbours */
    neighbours(i).forEach(ensure);
    if (!firstMountDone) { firstMountDone = true; onFirstMount && onFirstMount(); }
  }

  /* ---- navigation targets */
  function nextMain(i) { for (let k = i + 1; k < n; k++) if (!items[k].backup) return k; return -1; }
  function prevMain(i) { for (let k = i - 1; k >= 0; k--) if (!items[k].backup) return k; return -1; }
  function below(i) { return i + 1 < n && items[i + 1].backup ? i + 1 : -1; }
  function above(i) { return i >= 0 && i < n && items[i].backup && i > 0 ? i - 1 : -1; }
  function lastMain() { const k = prevMain(n); return k >= 0 ? k : n - 1; }

  /* letterbox: integer offsets keep text on whole pixels */
  function fit() {
    if (isPrint) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const s = Math.min(vw / W, vh / H);
    const x = Math.round((vw - W * s) / 2);
    const y = Math.round((vh - H * s) / 2);
    root.style.setProperty("--stage-scale", String(s));
    root.style.setProperty("--stage-x", x + "px");
    root.style.setProperty("--stage-y", y + "px");
  }

  /* ---- counters (identical to stage.js v1) */
  function parseCount(el) {
    const text = el.getAttribute("data-count-text") || el.textContent;
    el.setAttribute("data-count-text", text);
    const m = text.match(/-?[\d,]*\.?\d+/);
    if (!m) return null;
    const raw = m[0];
    const dec = (raw.split(".")[1] || "").length;
    return {
      pre: text.slice(0, m.index),
      post: text.slice(m.index + raw.length),
      to: parseFloat(raw.replace(/,/g, "")),
      from: parseFloat(el.getAttribute("data-count-from") || "0"),
      dec,
      sep: raw.indexOf(",") >= 0,
      text,
    };
  }
  function fmt(v, c) {
    let s = v.toFixed(c.dec);
    if (c.sep) {
      const parts = s.split(".");
      parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
      s = parts.join(".");
    }
    return c.pre + s + c.post;
  }
  function delayOf(el) {
    const d = getComputedStyle(el).getPropertyValue("--d").trim();
    if (!d) return 0;
    return d.endsWith("ms") ? parseFloat(d) : parseFloat(d) * 1000;
  }
  function runCounters(slide) {
    slide.querySelectorAll("[data-count]").forEach((el) => {
      const c = parseCount(el);
      if (!c) return;
      const prev = counters.get(el);
      if (prev) cancelAnimationFrame(prev.raf);
      if (isStatic) { el.textContent = c.text; return; }
      const dur = parseFloat(el.getAttribute("data-count-dur") || "1600");
      const start = performance.now() + delayOf(el);
      const state = { raf: 0 };
      counters.set(el, state);
      el.textContent = fmt(c.from, c);
      (function tick(now) {
        const t = Math.min(1, Math.max(0, (now - start) / dur));
        const e = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);          /* ease-out-expo */
        el.textContent = t >= 1 ? c.text : fmt(c.from + (c.to - c.from) * e, c);
        if (t < 1) state.raf = requestAnimationFrame(tick);
      })(performance.now());
    });
  }

  /* ---- video: only the current slide's clips run, from the start; loop is enforced */
  function guardVideo(v) {
    if (v.__exoGuard) return;
    v.__exoGuard = true;
    v.muted = true;
    v.addEventListener("ended", () => {
      const k = items.findIndex((it) => it.el.contains(v));
      if (k === current && !isStatic && (v.loop || v.hasAttribute("loop"))) {
        try { v.currentTime = 0; } catch (err) { /* not seekable yet */ }
        const p = v.play();
        if (p && p.catch) p.catch(() => {});
      }
    });
  }
  function syncVideo() {
    items.forEach((it, k) => {
      if (!it.mounted) return;
      it.el.querySelectorAll("video").forEach((v) => {
        if (k === current && !isStatic) {
          try { v.currentTime = 0; } catch (err) { /* not seekable yet */ }
          const p = v.play();
          if (p && p.catch) p.catch(() => {});
        } else if (!v.paused) {
          v.pause();
        }
      });
    });
  }

  /* ---- navigation */
  function clearV() {
    clearTimeout(vTimer);
    while (vMoved.length) {
      const el = vMoved.pop();
      el.classList.remove("v-in-down", "v-out-down", "v-in-up", "v-out-up");
    }
  }
  function go(i, o) {
    if (!n) return;
    i = Math.max(0, Math.min(n - 1, i | 0));
    const replay = !(o && o.replay === false);
    if (i === current && !(o && o.force)) return;
    mountAround(i);
    clearV();
    const prev = items[current] && items[current].el;
    if (prev) { prev.classList.remove("is-current", "is-active"); prev.setAttribute("aria-hidden", "true"); }
    const next = items[i].el;
    next.classList.remove("is-active");
    void next.offsetWidth;                                   /* restart CSS animations */
    const dir = o && o.dir;
    if (dir && prev && prev !== next && !isStatic) {
      next.classList.add("v-in-" + dir);
      prev.classList.add("v-out-" + dir);
      vMoved.push(next, prev);
      vTimer = setTimeout(clearV, 900);
    }
    next.classList.add("is-current");
    next.removeAttribute("aria-hidden");
    if (replay) next.classList.add("is-active");
    current = i;
    runCounters(next);
    syncVideo();
    const hash = "#" + (i + 1);
    if (location.hash !== hash) {
      // CSP sandbox gives hosted pages an opaque origin; replaceState may throw.
      try { history.replaceState(null, "", location.pathname + location.search + hash); }
      catch (e) { location.hash = hash; }
    }
    document.dispatchEvent(new CustomEvent("showcase:slide", { detail: { index: i, slide: next } }));
  }
  function next() { const k = nextMain(current); if (k >= 0) go(k); }
  function prev() { const k = prevMain(current); if (k >= 0) go(k); else go(current - 1); }
  function down() { const k = below(current); if (k >= 0) go(k, { dir: "down" }); else next(); }
  function up() { const k = above(current); if (k >= 0) go(k, { dir: "up" }); else prev(); }
  function fromHash() {
    const v = parseInt((location.hash || "").replace("#", ""), 10);
    return isNaN(v) ? 0 : v - 1;
  }

  function onKey(e) {
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    switch (e.key) {
      case "ArrowRight": case "PageDown": case " ": case "Spacebar":
        e.preventDefault(); next(); break;
      case "ArrowDown":
        e.preventDefault(); down(); break;
      case "ArrowLeft": case "PageUp":
        e.preventDefault(); prev(); break;
      case "ArrowUp":
        e.preventDefault(); up(); break;
      case "Home": e.preventDefault(); go(0); break;
      case "End": e.preventDefault(); go(lastMain()); break;
      case "f": case "F":
        if (!document.fullscreenElement && root.requestFullscreen) root.requestFullscreen().catch(() => {});
        else if (document.exitFullscreen) document.exitFullscreen().catch(() => {});
        break;
      case "r": case "R": go(current, { force: true }); break;   /* replay the entrance */
      case "s": case "S": case "0": go(0); break;
      case "e": case "E": case "9": go(lastMain()); break;
      default:
        if (/^[1-8]$/.test(e.key)) go(Math.min(+e.key, n) - 1);
    }
  }

  function onClick(e) {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target && e.target.closest && e.target.closest("a, button, input, select, textarea")) return;
    if (e.shiftKey) prev(); else next();
  }

  const hasBackups = items.some((it) => it.backup);
  let tx = 0, ty = 0, tt = 0;
  function onTouchStart(e) { const p = e.changedTouches[0]; tx = p.clientX; ty = p.clientY; tt = Date.now(); }
  function onTouchEnd(e) {
    const p = e.changedTouches[0];
    const dx = p.clientX - tx;
    const dy = p.clientY - ty;
    if (Date.now() - tt > 800) return;
    if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.4) { if (dx < 0) next(); else prev(); }
    else if (hasBackups && Math.abs(dy) > 48 && Math.abs(dy) > Math.abs(dx) * 1.4) { if (dy < 0) down(); else up(); }
  }

  function start() {
    window.ShowcaseStage = {
      go: (i) => go(i, { force: true }),
      next, prev, down, up,
      index: () => current,
      count: () => n,
    };
    if (!n) { root.classList.remove("is-pending"); return; }
    if (isPrint) {
      for (let i = 0; i < n; i++) ensure(i);
      if (!firstMountDone) { firstMountDone = true; onFirstMount && onFirstMount(); }
      items.forEach((it) => { it.el.classList.add("is-current"); it.el.removeAttribute("aria-hidden"); });
      return;
    }
    fit();
    window.addEventListener("resize", fit);
    document.addEventListener("keydown", onKey);
    document.addEventListener("click", onClick);
    document.addEventListener("touchstart", onTouchStart, { passive: true });
    document.addEventListener("touchend", onTouchEnd, { passive: true });
    window.addEventListener("hashchange", () => go(fromHash()));
    /* show the first slide immediately (static), then play its entrance once the fonts
       are ready so nothing animates in a fallback face */
    const first = Math.max(0, Math.min(n - 1, fromHash()));
    mountAround(first);
    items[first].el.classList.add("is-current");
    let begun = false;
    const begin = () => {
      if (begun) return;
      begun = true;
      current = -1;
      go(first, { force: true });
      root.classList.remove("is-pending");
    };
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(begin);
      setTimeout(begin, 1200);
    } else begin();
  }

  return { start, isPrint, isStatic, folios };
}
