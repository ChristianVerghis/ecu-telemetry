#!/usr/bin/env node
// Dashboard screenshots and frame sequences from headless Chrome over the DevTools protocol.
// No dependencies: Node 22+ (global fetch and WebSocket) and a local Chrome.
//
//   node tools/capture.mjs shot   URL OUT.png  [--size 1440x1000] [--scale 2] [--wait 6000]
//   node tools/capture.mjs record BASE OUTDIR  [--size 1280x800] [--every 2] [--for 186]
//                                              [--plan "0:#/,22:#/device/ECU-0001,46:#/"]
//
// `record` waits for BASE/health, then saves OUTDIR/frame-NNN.png every --every seconds and switches
// the page to each hash in --plan at its second. Turn the frames into a GIF with ffmpeg:
//
//   ffmpeg -framerate 6 -i OUTDIR/frame-%03d.png -vf "scale=960:-1:flags=lanczos,split[a][b];\
//     [a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer:bayer_scale=4" docs/img/demo.gif
//
// Chrome's own `--screenshot` flag is not enough here: it fires before the charts have data, and
// `--virtual-time-budget` never returns on a page that holds a server-sent-events stream open.
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const [mode, target, out, ...rest] = process.argv.slice(2);
const opt = (name, dflt) => { const i = rest.indexOf(`--${name}`); return i >= 0 ? rest[i + 1] : dflt; };
if (!["shot", "record"].includes(mode) || !target || !out) {
  console.error("usage: capture.mjs shot URL OUT.png | record BASE OUTDIR  (see the header for options)");
  process.exit(2);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const [w, h] = opt("size", mode === "shot" ? "1440x1000" : "1280x800").split("x").map(Number);
const scale = Number(opt("scale", mode === "shot" ? 2 : 1));

const port = 9300 + Math.floor(Math.random() * 500);
const chrome = spawn(CHROME, [
  "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), "capture-"))}`, `--window-size=${w},${h}`, "about:blank",
], { stdio: "ignore" });

let rc = 0;
try {
  let page;
  for (let i = 0; i < 50 && !page; i++) {
    await sleep(200);
    try { page = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find((t) => t.type === "page"); } catch {}
  }
  if (!page) throw new Error(`Chrome did not open a DevTools port (${CHROME})`);
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) { pending.get(d.id)(d.result); pending.delete(d.id); }
  });
  const send = (method, params = {}) => new Promise((r) => { pending.set(++id, r); ws.send(JSON.stringify({ id, method, params })); });
  const png = async (path) => writeFileSync(path, Buffer.from((await send("Page.captureScreenshot", { format: "png" })).data, "base64"));
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: scale, mobile: false });

  if (mode === "shot") {
    await send("Page.navigate", { url: target });
    await sleep(Number(opt("wait", 6000)));
    await png(out);
    console.log("wrote", out);
  } else {
    const base = target.replace(/\/$/, "");
    const every = Number(opt("every", 2)), total = Number(opt("for", 186));
    const plan = opt("plan", "0:#/").split(",").map((s) => { const i = s.indexOf(":"); return [Number(s.slice(0, i)), s.slice(i + 1)]; });
    mkdirSync(out, { recursive: true });
    for (let i = 0; ; i++) {   // the demo's backend may still be starting
      try { if ((await fetch(`${base}/health`)).ok) break; } catch {}
      if (i > 150) throw new Error(`${base}/health never answered`);
      await sleep(200);
    }
    const t0 = Date.now();
    await send("Page.navigate", { url: `${base}/${plan[0][1]}` });
    let step = 0, n = 0;
    for (let t = every; t <= total; t += every) {
      await sleep(Math.max(0, t0 + t * 1000 - Date.now()));
      await png(join(out, `frame-${String(n++).padStart(3, "0")}.png`));
      while (step + 1 < plan.length && t >= plan[step + 1][0]) {
        step++;
        await send("Runtime.evaluate", { expression: `location.hash = ${JSON.stringify(plan[step][1])}` });
      }
    }
    console.log(`wrote ${n} frames to ${out}`);
  }
} catch (e) {
  console.error(String(e.message || e));
  rc = 1;
} finally {
  chrome.kill();
}
process.exit(rc);
