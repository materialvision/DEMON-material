// Portable, hand-authored USER-FACING control copy: the prose tooltips and
// display names a DEMON frontend shows on its knobs and inputs. Keyed by the
// same param ids the backend registry uses, but the copy here is editorial
// ("what musical outcome this produces / when to reach for it"), NOT the
// terse, agent-facing one-liners the backend serves at /api/knobs
// (KnobManifestEntry.description). The two are intentionally distinct
// audiences; controls/index.ts `resolveControlDescription` is the merge point
// when a consumer wants the rich copy with a graceful fallback to the manifest
// for knobs that only exist at runtime (LoRAs, manual slots).
//
// This mirrors the config/ module's "client-side, hand-authored, distinct from
// the generated contracts" stance. What stays per-client: the DOM/tooltip
// rendering machinery (useTooltipHover, the help readouts) and web-only
// affordance hints (keyboard chords, MIDI-learn, kiosk/LUFS toggles).

// Display names — anchored in traditional audio vocabulary (synth /
// multi-FX / EQ heritage) so the labels read instantly to anyone who's
// touched a hardware unit or plugin. Where the underlying concept has
// no clean analog (shift, noise_share), the technical label stays.
// Hosts typically uppercase these on render.
export const CONTROL_DISPLAY_NAMES: Record<string, string> = {
  // Macros where the friendly name reads more clearly than the
  // engine-honest one: `strength` for `denoise` (the "denoise"
  // engine-internal naming refers to the diffusion step, but users
  // perceive this knob as "how strong is the remix"), `structure` for
  // `hint_strength`, `timbre` for `timbre_strength` (drop the
  // "strength" suffix on knobs — the value readout already conveys
  // magnitude). Everything else falls back to displayNameFor
  // (underscore → space) so the UI matches what the engine, MIDI map,
  // and config files call them.
  denoise: "strength",
  hint_strength: "structure",
  timbre_strength: "timbre",
  // dcw_* keep their engine-honest "DCW low" / "DCW high" — these are
  // DCW-internal scalers, not generic EQ.
  dcw_scaler: "DCW low",
  dcw_high_scaler: "DCW high",
};

// Tooltip copy for each tweakable param, surfaced via the slider/knob label's
// hover tooltip. Aim: a 1–2 second read that tells the user WHEN to reach for
// this knob — what musical outcome it produces, not the diffusion-process
// plumbing underneath.
export const CONTROL_DESCRIPTIONS: Record<string, string> = {
  // ── Main remix controls ──
  denoise:
    "How much the model reshapes the source audio. Keep it low for a subtle remix that stays close to the original; push it high to fully transform the track into something new. The most expressive knob — try sweeping it during playback.",
  hint_strength:
    "How closely the model follows the original song's structure — sections, rhythm, dynamics. Crank it up to keep the arrangement intact; drop it to let the model rearrange more freely.",
  timbre_strength:
    "How much of the source's instrument character (tone, color) carries into the output. High keeps the original instruments recognizable; low frees the model to swap them for whatever fits the prompt.",

  // ── Engine internals ──
  feedback:
    "How similar each new generation is to the previous one. Low values give you variety on every refresh; higher values give you a continuous evolution where each generation flows into the next. 0.3–0.5 is the sweet spot for smooth continuity without everything sounding the same.",
  feedback_depth:
    "How far back in time the Feedback knob reaches. 1 (default) blends with the most recent generation. Higher values reach back several ticks for an echo / ghost effect — a faint repeat of an earlier moment surfaces in the current output. Lets you get distant feedback without cranking Feedback all the way up.",
  shift:
    "Advanced: changes where the model concentrates its work across denoising. The default is tuned for the turbo engine and works well in most cases — leave it alone unless you're chasing a specific feel.",
  steps_override:
    "Diffusion step count. Lower steps = lower quality. Higher steps = more latency. Default 8 is the turbo balance. Changing this rebuilds the streaming pipeline, so expect a brief audio glitch when you move it.",
  guidance_scale:
    "CFG strength. Only takes effect when the RCFG mode dropdown below is NOT 'off'. Higher values push the output further toward the prompt at the cost of more artifacts. Turbo is CFG-distilled, so the useful range is narrower than a base SD model — try 3–8.",
  cfg_rescale:
    "After CFG, mix the guided velocity's magnitude back toward what the positive forward produced. 0 keeps raw CFG; 1 fully snaps the magnitude. Pair with high guidance_scale to keep the prompt-push without the harshness that high CFG causes on its own.",

  // ── Activation steering (auto path) ──
  // Each tooltip names the underlying probe cell so the operator can
  // recreate the effect on a manual slot.
  steer_bright:
    "Activation-steering: positive alpha shifts spectral centroid up (brighter, more highs). 0 = off; useful range 5-15 by ear. Recreate as a manual slot: vector brightness_l09_t3 at layer = 9, step = round(3/8 x steps_count).",
  steer_warm:
    "Activation-steering: positive alpha tilts the spectrum toward bass (warmer). The raw vector points the wrong way for this axis, so this knob folds in a -1 sign. 0 = off; useful range 5-15 by ear. Recreate as a manual slot: vector warmth_l15_t0 at layer = 15, step = 0, then INVERT alpha sign (manual mode is sign-agnostic).",
  steer_rough:
    "Activation-steering: positive alpha increases spectral flatness (grittier, noisier). Vector magnitude at this probe cell is small, so effect builds slowly. 0 = off; useful range 5-15 by ear. Recreate as a manual slot: vector roughness_l09_t3 at layer = 9, step = round(3/8 x steps_count).",
  steer_density:
    "Activation-steering: positive alpha thins the texture toward sparse/minimal. Inject layer is shifted 3 shallower than the probe layer (Phase-3 transfer finding). 0 = off; useful range 5-15 by ear. Recreate as a manual slot: vector density_l18_t3 at layer = 15 (probe 18 minus 3), step = round(3/8 x steps_count).",

  // ── DCW ──
  dcw_scaler:
    "Experimental — adjusts the low-band strength of an internal correction the model applies to itself during generation (DCW). This scaler is active in the early part of the run. The exact audio mapping is still being explored — sweep it to discover what it does for your source. Extreme values can be unpredictable but cool.",
  dcw_high_scaler:
    "Experimental — adjusts the high-band strength of an internal correction the model applies to itself during generation (DCW). This scaler is active in the later part of the run. The exact audio mapping is still being explored — sweep it to discover what it does for your source. Extreme values can be unpredictable but cool.",
};

// Per-channel tooltips. The 64-channel latent space hasn't been fully
// mapped to perceptual qualities yet, so the copy frames each channel
// as something to discover by ear — not a labeled knob with a known
// purpose. Generated programmatically to avoid 14 near-identical
// hand-written strings.
const CHANNEL_GAINS = ["ch_g0", "ch_g1", "ch_g2", "ch_g3", "ch_g4", "ch_g5", "ch_g6", "ch_g7"] as const;
const NAMED_CHANNELS = ["ch13", "ch14", "ch19", "ch23", "ch29", "ch56"] as const;
for (const [i, p] of CHANNEL_GAINS.entries()) {
  CONTROL_DESCRIPTIONS[p] =
    `Experimental — adjusts the strength of one of the model's internal latent channels (channel ${i}). Each channel encodes a different aspect of the sound (frequency band, dynamics, transients); the exact mapping is still being explored. Sweep it to discover what it does for your source.`;
}
for (const p of NAMED_CHANNELS) {
  const idx = p.slice(2);
  CONTROL_DESCRIPTIONS[p] =
    `Experimental — a hand-picked internal latent channel (#${idx}) that produces a noticeable perceptual change. Sweep it to hear what this specific channel controls for your source.`;
}

// Prefix-matched copy for the runtime-generated knob families (one shared
// string across every LoRA / manual-steering slot). describeControl()
// reaches for these before the static map.
export const LORA_STRENGTH_DESCRIPTION =
  "How strongly this LoRA shapes the output. LoRAs are little style packs — set a low value for a subtle flavor, crank past 1.0 to make this LoRA dominate the sound. Multiple LoRAs stack — turn several on at once for combined styles.";
export const LORA_BLEND_DESCRIPTION =
  "Crossfade between LoRA A and LoRA B. 0 = A only, 1 = B only, 0.5 = both at half strength. Use this to morph between two styles smoothly.";
export const MANUAL_SRC_DESCRIPTION =
  "Catalog index of the steering vector this slot fires. The catalog enumerates every pre-built (axis, build_layer, build_step) cell on disk in stable axis-major order. Double-click the readout to type an exact index; query the MCP list_manual_steering_vectors tool for the full table. Has no effect until α is non-zero.";
export const MANUAL_LAYER_DESCRIPTION =
  "DiT inject layer (0-23). The vector is added to this layer's post-block residual. Bypasses the auto path's density layer offset — the value lands exactly where you point it.";
export const MANUAL_STEP_DESCRIPTION =
  "Diffusion inject step (0-15). Bypasses the auto path's fractional step mapping; the engine fires the injection only on the step that matches this value. If you pick a step past the current steps count - 1, the slot stays silent until you raise the step count.";
export const MANUAL_ALPHA_DESCRIPTION =
  "Strength of this manual slot's injection. 0 disables the slot. Negative α inverts the vector's direction at injection time (no sign correction is applied; what you set is what the engine receives). Sweep range and breakage point mirror the perceptual steering knobs.";

// Interpolation-method copy, keyed by blend path (structure / timbre /
// prompt / feedback). Explains what slerp-vs-linear does for each lane.
export const INTERP_PATH_DESCRIPTIONS: Record<string, string> = {
  structure:
    "How structural (semantic-hint) guidance blends in. Slerp holds the latent's norm constant; linear averages.",
  timbre:
    "How the timbre reference blends from silence to full. Slerp holds the conditioning norm constant; linear averages.",
  prompt:
    "How prompt A crossfades to prompt B. Slerp avoids the washed-out midpoint a linear average produces between unrelated prompts.",
  feedback:
    "How the latent feedback tap mixes into the source. Slerp holds the latent's norm constant; linear averages.",
};

// Inference-source hints, keyed by stem source mode.
export const SOURCE_MODE_HINTS: Record<string, string> = {
  full: "Feed the whole mix to inference",
  instruments: "Feed only the instrumental bed to inference",
  vocals: "Feed only the vocal stem to inference",
};

// Reference-audio picker copy for the timbre / structure channels.
export const TIMBRE_REF_DESCRIPTION =
  "Optional reference audio for the timbre channel. Picking a track here biases the model's instrument character toward what's in that file, leaving structure (rhythm, sections) free to follow the playing input. Default 'Input Track' uses the playing source's own latent.";
export const STRUCTURE_REF_DESCRIPTION =
  "Optional reference audio for the structure channel. Picking a track here biases the model's section/rhythm/dynamics layout toward that file, leaving timbre (instrument character) free to follow the playing input. Default 'Input Track' uses the playing source's own latent.";

// Stem-layer section copy (the vocal/instrumental panners overview).
export const STEM_SECTION_DESCRIPTION =
  "Vocal and instrumental stems extracted from the source track. Drag a panner right to mix that layer into the model output. Click the layer name to mute or unmute without losing the level. Hold V (vocals) or I (instruments) + ▲▼ to nudge from the keyboard.";
