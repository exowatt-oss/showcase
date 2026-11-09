/* The v1 layouts. Each component renders the content of one slide shell, once.
   Classes: layout-<name> + a ground (on-paper | on-bright | on-light | on-ink | on-dusk) are set
   on the shell by mount.jsx; everything inside is prefixed lt-. Typography, grounds, frames,
   scrims and light come from the neutral viewer theme (theme-neutral.css). */

import { T, Media, Frame, Foot, Notes, Kicker, asMedia, sized, countable, splitUnit } from "./parts.jsx";

const pad = (n) => (n < 10 ? "0" : "") + n;
const ms = (v) => "--d: " + v + "ms";

/* ---------------------------------------------------------------- title */
function Title({ s, fine }) {
  const m = asMedia(s.media);
  return (
    <>
      {m ? (
        <Frame media={m} cls="bleed bleed-full lt-bg" reveal="settle">
          <i class="scrim lt-scrim-title" />
        </Frame>
      ) : (
        <>
          <div class="horizon-light lt-horizon" aria-hidden="true" />
          <div class="glow lt-glow" aria-hidden="true" />
        </>
      )}
      <div class="lt-title-copy">
        <Kicker text={s.kicker} />
        <h1 class={"lt-h " + sized(s.title, [[40, "t-display"], [Infinity, "t-h1"]])} data-reveal="mask" style={ms(300)}>
          <T text={s.title} />
        </h1>
        {s.subtitle ? (
          <p class="t-lead lt-sub" data-reveal style={ms(650)}><T text={s.subtitle} /></p>
        ) : null}
      </div>
      <Foot fine={fine} />
    </>
  );
}

/* ---------------------------------------------------------------- statement */
function Statement({ s, fine }) {
  return (
    <>
      <div class="glow glow--soft lt-glow" aria-hidden="true" />
      <div class="lt-statement">
        <Kicker text={s.kicker} />
        <div class="lt-statement-body">
          <h1 class={"lt-h " + sized(s.title, [[26, "t-display lt-mega"], [64, "t-display"], [Infinity, "t-h1"]])} data-reveal="mask" style={ms(260)}>
            <T text={s.title} />
          </h1>
          {s.body ? <p class="t-lead lt-body" data-reveal style={ms(620)}><T text={s.body} /></p> : null}
        </div>
      </div>
      <Foot fine={fine} />
    </>
  );
}

/* ---------------------------------------------------------------- hero-media */
function HeroMedia({ s, fine }) {
  const side = s.side === "right" ? "right" : "left";
  return (
    <>
      <Frame media={s.media} cls="bleed bleed-full lt-bg" reveal="settle">
        <i class={"scrim lt-scrim-side is-" + side} />
        <i class="scrim lt-scrim-foot" />
      </Frame>
      <div class={"lt-hero-copy is-" + side}>
        <Kicker text={s.kicker} />
        <h2 class={"lt-h " + sized(s.title, [[44, "t-h1"], [Infinity, "t-h2"]])} data-reveal="mask" style={ms(320)}>
          <T text={s.title} />
        </h2>
        {s.body ? <p class="t-body lt-body" data-reveal style={ms(640)}><T text={s.body} /></p> : null}
      </div>
      <Foot fine={fine} />
    </>
  );
}

/* ---------------------------------------------------------------- split */
function Split({ s, fine }) {
  const side = s.side === "left" ? "left" : "right";
  return (
    <>
      <Frame media={s.media} cls={"lt-split-media is-" + side} reveal="settle" />
      <div class={"lt-split-copy is-" + side}>
        <div class="lt-split-top">
          <Kicker text={s.kicker} />
          <h2 class={"lt-h " + sized(s.title, [[48, "t-h1"], [Infinity, "t-h2"]])} data-reveal="mask" style={ms(300)}>
            <T text={s.title} />
          </h2>
        </div>
        {s.body ? (
          <p class={"lt-body " + sized(s.body, [[150, "t-lead"], [Infinity, "t-body"]])} data-reveal style={ms(640)}>
            <T text={s.body} />
          </p>
        ) : null}
      </div>
      <Foot fine={fine} />
    </>
  );
}

/* ---------------------------------------------------------------- stats */
function Stats({ s, fine }) {
  const stats = (Array.isArray(s.stats) ? s.stats : []).slice(0, 4);
  const m = asMedia(s.media);
  return (
    <>
      {m ? <Frame media={m} cls="lt-stats-media" reveal="settle" /> : null}
      <div class={"lt-stats n-" + stats.length + (m ? " has-media" : "")}>
        <header class="lt-stats-head">
          <Kicker text={s.kicker} />
          <h2 class={"lt-h " + sized(s.title, [[56, "t-h1"], [Infinity, "t-h2"]])} data-reveal="mask" style={ms(260)}>
            <T text={s.title} />
          </h2>
          {s.body ? <p class="t-body lt-body" data-reveal style={ms(520)}><T text={s.body} /></p> : null}
        </header>
        <div class="lt-stats-row">
          {stats.map((st, i) => {
            const [num, unit] = splitUnit(st.value);
            const d = 560 + i * 130;
            return (
              <div class={"lt-stat" + (i === 0 ? " is-lead" : "")} data-reveal style={ms(d)}>
                <i class={"lt-stat-rule " + (i === 0 ? "rule-ember" : "rule-strong")} aria-hidden="true" />
                <div class="lt-stat-fig" data-fit>
                  <span class="lt-fit">
                    <span class="lt-stat-num" data-count={countable(num) ? "" : undefined} style={ms(d)}>{num}</span>
                    {unit ? <span class="lt-stat-unit">{unit}</span> : null}
                  </span>
                </div>
                <p class="lt-stat-label"><T text={st.label} /></p>
                {st.sub ? <p class="lt-stat-sub"><T text={st.sub} /></p> : null}
              </div>
            );
          })}
        </div>
      </div>
      <Foot fine={fine} />
    </>
  );
}

/* ---------------------------------------------------------------- steps */
function Steps({ s, fine }) {
  const steps = (Array.isArray(s.steps) ? s.steps : []).slice(0, 6);
  const n = steps.length;
  const anyMedia = steps.some((st) => asMedia(st.media));
  return (
    <>
      <header class="lt-steps-head">
        <Kicker text={s.kicker} />
        <h2 class={"lt-h " + sized(s.title, [[60, "t-h2"], [Infinity, "t-h3"]])} data-reveal="mask" style={ms(260)}>
          <T text={s.title} />
        </h2>
      </header>
      <div class={"lt-steps-line" + (anyMedia ? " has-media" : "")} data-reveal="rule" style={ms(380)} aria-hidden="true" />
      <ol class={"lt-steps n-" + n + (anyMedia ? " has-media" : "")}>
        {steps.map((st, i) => (
          <li class="lt-step" data-reveal style={ms(520 + i * 140)}>
            {anyMedia ? (
              <div class="lt-step-media">
                {asMedia(st.media) ? <Frame media={st.media} cls="lt-step-frame" alt={st.title} /> : null}
              </div>
            ) : null}
            <span class="lt-step-tick" aria-hidden="true" />
            <span class={"lt-step-n" + (i === n - 1 ? " t-ember" : "")}>{pad(i + 1)}</span>
            <h3 class="lt-step-title"><T text={st.title} /></h3>
            {st.body ? <p class="lt-step-body"><T text={st.body} /></p> : null}
          </li>
        ))}
      </ol>
      <Foot fine={fine} />
    </>
  );
}

/* ---------------------------------------------------------------- gallery */
function Gallery({ s, fine }) {
  const items = (Array.isArray(s.items) ? s.items : []).slice(0, 8);
  const n = items.length;
  const head = !!(s.title || s.kicker);
  return (
    <>
      {head ? (
        <header class="lt-gallery-head">
          <Kicker text={s.kicker} />
          {s.title ? (
            <h2 class="lt-h t-h2" data-reveal="mask" style={ms(260)}><T text={s.title} /></h2>
          ) : null}
        </header>
      ) : null}
      <div class={"lt-gallery n-" + n + (head ? " has-head" : "")}>
        {items.map((it, i) => (
          <div class="lt-gal-item" data-reveal style={ms(420 + i * 110)}>
            <Frame media={it.media} cls="lt-gal-frame" alt={it.caption} />
            {it.caption ? <p class="lt-gal-cap"><T text={it.caption} /></p> : null}
          </div>
        ))}
      </div>
      <Foot fine={fine} />
    </>
  );
}

/* ---------------------------------------------------------------- quote */
const QUOTES = /^[\s"“”'‘’«»]+|[\s"“”'‘’«»]+$/g;
function Quote({ s, fine }) {
  const m = asMedia(s.media);
  const text = String(s.quote || "").replace(QUOTES, "");
  return (
    <>
      {m ? <Frame media={m} cls="lt-quote-media" reveal="settle" /> : <div class="glow lt-glow" aria-hidden="true" />}
      <figure class={"lt-quote" + (m ? " has-media" : "")}>
        <blockquote class={"lt-q " + sized(text, [[64, "is-xl"], [130, "is-l"], [230, "is-m"], [Infinity, "is-s"]])} data-reveal="mask" style={ms(260)}>
          <p>
            <span class="lt-qmark t-ember" aria-hidden="true">“</span>
            <T text={text} />
            <span aria-hidden="true">”</span>
          </p>
        </blockquote>
        {s.by ? (
          <figcaption class="kicker t-micro lt-by" data-reveal="fade" style={ms(760)}><span><T text={s.by} /></span></figcaption>
        ) : null}
      </figure>
      <Foot fine={fine} />
    </>
  );
}

/* ---------------------------------------------------------------- media */
function MediaFull({ s, fine }) {
  return (
    <>
      <Frame media={s.media} cls="bleed bleed-full lt-bg" reveal="settle" alt={s.caption}>
        <i class="scrim lt-scrim-foot" />
      </Frame>
      {s.caption ? <p class="media-tag lt-media-cap" data-reveal="fade" style={ms(700)}><T text={s.caption} /></p> : null}
      <Foot fine={fine} />
    </>
  );
}

/* ---------------------------------------------------------------- close */
function Close({ s, fine }) {
  const pts = (Array.isArray(s.points) ? s.points : []).slice(0, 4);
  const m = asMedia(s.media);
  return (
    <>
      {m ? (
        <Frame media={m} cls="bleed bleed-full lt-bg" reveal="settle">
          <i class="scrim lt-scrim-close" />
        </Frame>
      ) : (
        <>
          <div class="horizon-light lt-horizon" aria-hidden="true" />
          <div class="glow lt-glow" aria-hidden="true" />
        </>
      )}
      <div class={"lt-close" + (pts.length ? " has-points" : "")}>
        <div class="lt-close-head">
          <Kicker text={s.kicker} />
          <h2 class={"lt-h " + sized(s.title, [[36, "t-display"], [Infinity, "t-h1"]])} data-reveal="mask" style={ms(300)}>
            <T text={s.title} />
          </h2>
        </div>
        {pts.length ? (
          <ol class="lt-points">
            {pts.map((p, i) => (
              <li data-reveal style={ms(620 + i * 120)}>
                <span class="lt-pt-n">{pad(i + 1)}</span>
                <span class="lt-pt-t"><T text={p} /></span>
              </li>
            ))}
          </ol>
        ) : null}
      </div>
      <Foot fine={fine} />
    </>
  );
}

/* ---------------------------------------------------------------- registry */
export const LAYOUTS = {
  title: { C: Title, tone: (s) => (asMedia(s.media) ? "dark" : "dusk"), bleed: (s) => !!asMedia(s.media) },
  statement: { C: Statement, tone: () => "paper" },
  "hero-media": { C: HeroMedia, tone: () => "dark", bleed: () => true },
  split: { C: Split, tone: () => "paper" },
  stats: { C: Stats, tone: () => "paper" },
  steps: { C: Steps, tone: () => "bright" },
  gallery: { C: Gallery, tone: () => "paper" },
  quote: { C: Quote, tone: (s) => (asMedia(s.media) ? "paper" : "dusk") },
  media: { C: MediaFull, tone: () => "dark", bleed: () => true },
  close: { C: Close, tone: (s) => (asMedia(s.media) ? "dark" : "dusk"), bleed: (s) => !!asMedia(s.media) },
};

export const TONES = { dark: "on-ink", light: "on-light", paper: "on-paper", bright: "on-bright", dusk: "on-dusk" };
export const DARK = { dark: true, dusk: true };

export { Notes, Media };
