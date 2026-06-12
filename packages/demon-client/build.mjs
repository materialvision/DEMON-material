// Builds the static-consumer browser bundle into dist/.
//
// Bundled apps (the rtmg Next demo) do NOT use this output — they import
// the TS source directly via the @demon/client alias and bundle it
// themselves. dist/ exists for no-build static demos (demos/arp) and any
// external page that wants a <script type="module"> SDK: one ESM bundle,
// the slice-decoder worker as a sibling file, and the audio worklet.
//
// dist/ is committed so pods and static hosts need no node toolchain.
// Regenerate after any SDK change: npm install && npm run build
import { copyFileSync, mkdirSync } from "node:fs";
import { build } from "esbuild";

const common = {
  bundle: true,
  format: "esm",
  target: "es2022",
  platform: "browser",
  sourcemap: false,
  logLevel: "info",
};

mkdirSync("dist", { recursive: true });

// Main SDK entry. fzstd is inlined: a static page has no package manager
// to satisfy the peer dependency. Consumers must pass
// RemoteBackendOptions.sliceWorkerUrl pointing at the sibling worker
// bundle (the source's `new URL(...ts...)` default only resolves under a
// transpiling bundler).
await build({
  ...common,
  entryPoints: ["index.ts"],
  outfile: "dist/demon-client.js",
});

// Slice-decoder worker, loaded by URL at runtime (fzstd inlined too).
await build({
  ...common,
  entryPoints: ["workers/sliceDecoder.worker.ts"],
  outfile: "dist/sliceDecoder.worker.js",
});

// The worklet is already plain JS; ship it alongside so static demos can
// point AudioPlayerOptions.workletUrl at the same /sdk/ mount.
copyFileSync("assets/audio-worklet.js", "dist/audio-worklet.js");
console.log("dist/audio-worklet.js copied");
