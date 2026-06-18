// Config-semantic enums — the value sets the operator-defaults `config.json`
// is authored against and validated by. Moved here from the web app
// (`web/types/engine.ts`, `web/lib/loopGrid.ts`) so all frontends share one
// source of truth: the schema in ./types.ts references these, and the web
// app re-exports them so its existing `@/types/engine` / `@/lib/loopGrid`
// call sites are unchanged.
//
// Why hand-defined here rather than projected from the generated wire types:
// the wire contract carries `time_signature` / `rcfg_mode` only as bare
// `string` (see types/wireContract.gen.ts) — there is no value union to
// source from — and `acestep.constants.VALID_TIME_SIGNATURES` is ints, not
// the end-to-end strings the encoder takes. So a single hand-authored copy,
// owned by the SDK and re-exported everywhere, is the drift-free home.
//
// UI-only metadata (slider bounds, display labels, keyscale lists) stays in
// the web app — it is presentation, not config semantics.

// ── Playback loop snap resolutions ("brace") ───────────────────────────
//
// Shared by the editor (WaveformScrubBox), the performance store, and
// config import/export so the set never drifts between them. Coarse→fine.

export type LoopGridRes = "bar" | "half" | "beat" | "eighth";

export const LOOP_GRID_ORDER: LoopGridRes[] = ["bar", "half", "beat", "eighth"];

/** Runtime guard — config JSON is operator-editable, so an imported grid
 *  value has to be validated before it's trusted as a `LoopGridRes`. */
export function isLoopGridRes(v: unknown): v is LoopGridRes {
  return typeof v === "string" && (LOOP_GRID_ORDER as string[]).includes(v);
}

// ── DCW (wavelet-domain post-step correction) mode + wavelet ───────────

export const DCW_MODES = ["low", "high", "double", "pix"] as const;
export const DCW_WAVELETS = ["haar", "db4", "sym8", "db8"] as const;
export type DcwMode = (typeof DCW_MODES)[number];
export type DcwWavelet = (typeof DCW_WAVELETS)[number];
export function isDcwMode(v: unknown): v is DcwMode {
  return typeof v === "string" && (DCW_MODES as readonly string[]).includes(v);
}
export function isDcwWavelet(v: unknown): v is DcwWavelet {
  return (
    typeof v === "string" && (DCW_WAVELETS as readonly string[]).includes(v)
  );
}

// ── RCFG (Residual Classifier-Free Guidance) modes ─────────────────────
//
// "off" disables APG entirely on the wire (turbo default — no guidance, no
// extra forwards). "initialize" runs the uncond pass only at step 0 per
// slot, caches the velocity, reuses it for the slot's remaining steps.
// "self" skips the uncond forward entirely; virtual ``v_uncond ≈
// initial_noise`` (flow-matching identity with ``x0_uncond ≈ 0``). See
// acestep/engine/stream.py. The engine also supports "full" (standard
// two-pass CFG, 2x cost), but it's intentionally NOT in the demo dropdown —
// turbo is CFG-distilled and an externally-driven full CFG against an
// empty-prompt uncond doesn't produce the right perceptual direction. Test
// scripts can still set ``rcfg_mode="full"`` directly.
export const RCFG_MODES = ["off", "initialize", "self"] as const;
export type RcfgMode = (typeof RCFG_MODES)[number];
export function isRcfgMode(v: unknown): v is RcfgMode {
  return typeof v === "string" && (RCFG_MODES as readonly string[]).includes(v);
}

// ── Time signature (meter numerator) ───────────────────────────────────
//
// Mirrors ``acestep.constants.VALID_TIME_SIGNATURES`` (``[2, 3, 4, 6]``).
// The encoder takes the value as a string in
// ``Session.encode_text(time_signature=...)``, where it gets baked into the
// prompt as ``- timesignature: <value>``. We carry the strings end-to-end so
// there's no int/string round-tripping at the wire.
export const VALID_TIME_SIGNATURES = ["2", "3", "4", "6"] as const;
export type TimeSignature = (typeof VALID_TIME_SIGNATURES)[number];

export const DEFAULT_TIME_SIGNATURE: TimeSignature = "4";

export function isTimeSignature(v: unknown): v is TimeSignature {
  return (
    typeof v === "string" &&
    (VALID_TIME_SIGNATURES as readonly string[]).includes(v)
  );
}
