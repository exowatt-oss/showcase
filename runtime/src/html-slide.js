/* Custom slides (layout "html"): the slide file holds one <section class="slide …"> plus
   optional sibling <style>/<link rel=stylesheet>/<script> elements, in any order.
   Mounting copies the section's attributes onto the stage shell (data-slide stays the slide
   id, data-backup stays what deck.json says), moves its content in, moves the styles into
   <head>, then runs every script once, in document order, as a fresh <script> element. */

import { runScripts } from "./scripts.js";

export function mountHtmlSlide(shell, source) {
  const tpl = document.createElement("template");
  tpl.innerHTML = source || "";
  const frag = tpl.content;
  let root = null;
  const styles = [];
  for (const node of Array.from(frag.childNodes)) {
    if (node.nodeType !== 1) continue;
    const tag = node.localName;
    if (tag === "section" && !root) root = node;
    else if (tag === "style" || (tag === "link" && /\bstylesheet\b/i.test(node.getAttribute("rel") || ""))) styles.push(node);
  }
  if (!root) {
    /* tolerate a fragment without the wrapping section: use everything that is not a style/script */
    root = document.createElement("section");
    for (const node of Array.from(frag.childNodes)) {
      if (node.nodeType === 1 && /^(style|script|link)$/.test(node.localName)) continue;
      root.appendChild(node);
    }
  }
  /* every script, in document order: nested ones are re-run in place, siblings after the stage */
  const scripts = Array.from(frag.querySelectorAll("script")).concat(
    root.parentNode ? [] : Array.from(root.querySelectorAll("script")),
  );
  styles.forEach((s) => document.head.appendChild(s));

  for (const a of Array.from(root.attributes)) {
    if (a.name === "data-slide" || a.name === "data-backup") continue;
    if (a.name === "class") {
      a.value.split(/\s+/).filter(Boolean).forEach((c) => shell.classList.add(c));
    } else {
      shell.setAttribute(a.name, a.value);
    }
  }
  while (root.firstChild) shell.appendChild(root.firstChild);

  runScripts(scripts.map((old) => ({ old, inShell: shell.contains(old) })));
}
