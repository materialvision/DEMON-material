// Node entry for the demon-client SDK — the surface a non-browser host
// (the Node-for-Max M4L bridge, headless tools) consumes. Deliberately
// narrower than index.ts: it omits AudioPlayer and the operator-defaults
// config loader so the CJS bundle pulls in zero browser-audio / DOM code
// and stays a single self-contained file (fzstd inlined by build.mjs).
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
