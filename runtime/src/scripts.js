/* Runs slide and deck scripts once, strictly in order. Inline scripts run synchronously the
   moment they are inserted (so a slide's script has run before the stage announces that
   slide); an external script holds the queue until it has loaded or failed. */

let tail = Promise.resolve();
let busy = 0;

function done() { busy -= 1; }

function enqueue(task) {
  if (busy === 0) {
    const r = task();
    if (r && typeof r.then === "function") { busy += 1; tail = r.then(done, done); }
    return;
  }
  busy += 1;
  tail = tail.then(() => task()).then(done, done);
}

function fresh(old) {
  const s = document.createElement("script");
  for (const a of Array.from(old.attributes)) s.setAttribute(a.name, a.value);
  s.textContent = old.textContent;
  return s;
}

function settle(s) {
  if (!s.hasAttribute("src")) return null;
  return new Promise((resolve) => { s.onload = resolve; s.onerror = resolve; });
}

/* list: [{ old: <script> (inert, from a template), inShell: bool }] */
export function runScripts(list) {
  list.forEach(({ old, inShell }) => {
    enqueue(() => {
      const s = fresh(old);
      const wait = settle(s);
      if (inShell && old.parentNode) old.parentNode.replaceChild(s, old);
      else {
        if (old.parentNode) old.parentNode.removeChild(old);
        document.body.appendChild(s);
      }
      return wait;
    });
  });
}

/* deck.scriptSources (inline JS text) or deck.scripts (URLs) */
export function runDeckScript(code, url, label) {
  enqueue(() => {
    const s = document.createElement("script");
    if (url) s.src = url;
    else s.textContent = code + "\n//# sourceURL=" + (label || "deck-script.js");
    s.setAttribute("data-deck-script", label || url || "");
    const wait = settle(s);
    document.body.appendChild(s);
    return wait;
  });
}
