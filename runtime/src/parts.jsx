/* Shared pieces for layout slides: inline text marks, media, footer, notes. */

/* Text fields are plain text with exactly two marks: **bold** and "\n" (line break).
   Everything becomes text nodes; nothing is parsed as HTML. */
export function inline(text) {
  if (text == null || text === false) return null;
  const out = [];
  String(text).split("\n").forEach((line, i) => {
    if (i) out.push(<br />);
    const parts = line.split("**");
    if (parts.length % 2 === 0) {                 /* an unpaired ** stays literal */
      const last = parts.pop();
      parts[parts.length - 1] += "**" + last;
    }
    parts.forEach((p, k) => {
      if (!p) return;
      out.push(k % 2 ? <b>{p}</b> : p);
    });
  });
  return out;
}

export function T({ text }) {
  return <>{inline(text)}</>;
}

/* plain string length (for size steps) */
export function len(text) {
  return String(text || "").replace(/\*\*/g, "").length;
}

/* pick a size class by text length: steps = [[maxChars, cls], …, [Infinity, cls]] */
export function sized(text, steps) {
  const L = len(text);
  for (const [max, cls] of steps) if (L <= max) return cls;
  return steps[steps.length - 1][1];
}

const VIDEO = /\.(mp4|webm|mov|m4v)(?:$|[?#])/i;

export function asMedia(media) {
  if (!media) return null;
  const m = typeof media === "string" ? { src: media } : { ...media };
  if (!m.src || typeof m.src !== "string") return null;
  m.video = VIDEO.test(m.src);
  m.fit = m.fit === "contain" ? "contain" : "cover";
  return m;
}

const PRINT = typeof location !== "undefined" && new URLSearchParams(location.search).has("print");

function videoRef(el) {
  if (!el) return;
  el.muted = true;
  el.defaultMuted = true;
  el.setAttribute("muted", "");
  el.setAttribute("playsinline", "");
}

/* <img decoding=async> or <video muted playsinline preload=metadata> */
export function Media({ media, cls, alt, focus }) {
  const m = asMedia(media);
  if (!m) return null;
  const style = { objectFit: m.fit };
  if (m.focus || focus) style.objectPosition = m.focus || focus;
  const label = m.alt != null ? m.alt : alt;
  if (m.video) {
    return (
      <video
        ref={videoRef}
        class={cls}
        src={m.src}
        poster={m.poster}
        muted
        playsInline
        preload={PRINT && !m.poster ? "auto" : "metadata"}
        loop={m.loop !== false}
        aria-label={label || undefined}
        style={style}
      />
    );
  }
  return <img class={cls} src={m.src} alt={label || ""} decoding="async" style={style} />;
}

/* a framed media box: <figure class="frame …"><img|video>…scrims</figure> */
export function Frame({ media, cls, reveal, delay, children, alt }) {
  const m = asMedia(media);
  if (!m) return null;
  return (
    <figure
      class={"frame lt-frame" + (m.fit === "contain" ? " is-contain" : "") + (cls ? " " + cls : "")}
      data-reveal={reveal}
      style={delay ? "--d: " + delay : undefined}
    >
      <Media media={m} alt={alt} />
      {children}
    </figure>
  );
}

export function Foot({ fine }) {
  return (
    <footer class="slide-foot lt-foot">
      {fine ? <span class="slide-foot__fine"><T text={fine} /></span> : null}
      <span class="slide-foot__folio" data-folio="auto" />
    </footer>
  );
}

export function Notes({ notes }) {
  if (!notes) return null;
  return <aside class="notes"><T text={notes} /></aside>;
}

export function Kicker({ text, delay = "120ms" }) {
  if (!text) return null;
  return (
    <p class="kicker t-micro lt-kicker" data-reveal="fade" style={"--d: " + delay}>
      <span><T text={text} /></span>
    </p>
  );
}

/* stat values: count up only when the value holds exactly one number */
export function countable(value) {
  const nums = String(value).match(/\d[\d,]*\.?\d*/g);
  return !!nums && nums.length === 1;
}

/* "0.631 ms" → ["0.631", "ms"]: a trailing word after a space is set as a smaller suffix */
export function splitUnit(value) {
  const v = String(value);
  const m = v.match(/^(.*\d[^\s]*)\s+([^\d\s][^\d]{0,11})$/);
  return m ? [m[1], m[2]] : [v, null];
}
