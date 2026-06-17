// Node entry for the demon-client SDK — the surface a non-browser host
// (the Node-for-Max M4L bridge, headless tools) consumes. Deliberately
// narrower than index.ts: it omits AudioPlayer and the operator-defaults
// config LOADER (`loadConfig`, the only `fetch`-coupled config file) so the
// CJS bundle pulls in zero browser-audio / DOM / network code and stays a
// single self-contained file (fzstd inlined by build.mjs). The portable
// config SCHEMA, transforms, and neutral state adapters ARE exported below —
// they are pure and the native preset path (M4L / VST) needs them.
//
// Built by `npm run build` → dist/demon-client.node.cjs (platform node,
// format cjs). The bridge `require()`s that artifact; see build.mjs.

export { RemoteBackend, float16ArrayToFloat32 } from "./protocol";
export type {
  RemoteBackendOptions,
  WsTrace,
  WsTracePhase,
} from "./protocol";

// Binary-framing constants a slice/canvas consumer needs (the 23-byte
// header layout and the delta flag) plus the fixed sample rate.
export {
  SAMPLE_RATE,
  SLICE_HDR_SIZE,
  SLICE_FLAG_RAW,
  SLICE_FLAG_DELTA,
  PREEMPTED_CLOSE_CODE,
} from "./types/protocol";
export type { AudioSlice } from "./types/protocol";

// Generated wire-contract payload types + name unions (erased at build,
// available to TS consumers that vendor the source).
export * from "./types/wireContract.gen";

// Portable `config.json` surface — the SAME source of truth the web app
// consumes, for the native preset path (M4L bridge / VST codegen). Mirrors
// config/index.ts MINUS `loadConfig` (the only `fetch`-coupled file), wired
// granularly so the bundler never pulls load.ts in. The store/DOM wiring
// stays per-client; these are the pure schema + transforms + neutral state
// adapters (`captureConfigFromState` / `applyConfigToState` operate on a
// host-supplied snapshot, not any store).
export * from "./config/enums";
export * from "./config/types";
export { DEFAULT_CONFIG } from "./config/defaults";
export {
  KNOWN_TOP_LEVEL_KEYS,
  getUnknownKeys,
  withUnknownKeys,
  serializeConfig,
  isSwapSourceMode,
  normalizeConfigShape,
  mergeConfig,
  resolveLoraCapForSource,
  selectVariant,
} from "./config/transforms";
export {
  rtmgConfigToSessionConfig,
  applyConfigToState,
  captureConfigFromState,
} from "./config/wire";
export type {
  SessionConfigRuntime,
  ConfigStateSnapshot,
  ConfigApplyPatch,
} from "./config/wire";
