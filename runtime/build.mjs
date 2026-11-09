// Build the Showcase deck viewer runtime into ../src/app/static/runtime/ (committed output;
// the deploy has no build step). Deterministic: same sources + same lockfile → same bytes.
//   node build.mjs          build
//   node build.mjs --check  build to a temp dir and fail if it differs from the committed output
import { build, transform } from "esbuild";
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, readdirSync, rmSync, copyFileSync, mkdtempSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { gzipSync } from "node:zlib";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const OUT = join(here, "..", "src", "app", "static", "runtime");
const check = process.argv.includes("--check");
const dest = check ? mkdtempSync(join(tmpdir(), "deck-runtime-")) : OUT;

const BANNER = "/* Showcase deck runtime · built by runtime/build.mjs — do not edit; edit runtime/src */";

async function main() {
  if (!check) rmSync(dest, { recursive: true, force: true });
  mkdirSync(dest, { recursive: true });

  const js = await build({
    entryPoints: [join(here, "src", "main.jsx")],
    bundle: true,
    format: "iife",
    target: ["es2020"],
    minify: true,
    legalComments: "none",
    charset: "utf8",
    jsx: "automatic",
    jsxImportSource: "preact",
    write: false,
    sourcemap: false,
    banner: { js: BANNER },
    define: { "process.env.NODE_ENV": '"production"' },
    logLevel: "warning",
  });
  writeFileSync(join(dest, "runtime.js"), js.outputFiles[0].contents);

  for (const name of ["stage.css", "theme-neutral.css"]) {
    const src = readFileSync(join(here, "css", name), "utf8");
    const out = await transform(src, { loader: "css", minify: true, target: ["chrome100", "safari16"], legalComments: "none", charset: "utf8" });
    writeFileSync(join(dest, name), BANNER + "\n" + out.code);
  }


  /* report sizes; --check compares every file byte for byte with the committed output */
  const list = (dir, rel = "") => readdirSync(dir, { withFileTypes: true })
    .sort((x, y) => (x.name < y.name ? -1 : 1))
    .flatMap((f) => (f.isDirectory() ? list(join(dir, f.name), rel + f.name + "/") : [rel + f.name]));
  const built = list(dest);
  for (const r of built) {
    if (!/\.(js|css)$/.test(r)) continue;
    const buf = readFileSync(join(dest, r));
    console.log(`${r.padEnd(20)} ${String(buf.length).padStart(7)} B  ${String(gzipSync(buf, { level: 9 }).length).padStart(6)} B gz`);
  }
  const h = createHash("sha256");
  for (const r of built) h.update(r + "\0").update(readFileSync(join(dest, r)));
  console.log(`content digest ${h.digest("hex").slice(0, 12)} → ${check ? "(check) " + dest : OUT}`);

  if (check) {
    const committed = existsSync(OUT) ? list(OUT) : [];
    const diff = [...new Set([...built, ...committed])].filter((r) =>
      !built.includes(r) || !committed.includes(r) || !readFileSync(join(dest, r)).equals(readFileSync(join(OUT, r))));
    rmSync(dest, { recursive: true, force: true });
    if (diff.length) {
      console.error("built runtime differs from src/app/static/runtime: " + diff.join(", ") + " — run `npm run build`");
      process.exit(1);
    }
    console.log("committed runtime is up to date");
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
