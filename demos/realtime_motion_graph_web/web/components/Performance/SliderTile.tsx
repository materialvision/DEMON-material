"use client";

import { SliderGroup } from "./SliderGroup";

// Generic mixer tile that wraps a row of sliders. Replaces the dynamic
// buildChannelTile() helper from app.js. `params` is the list of slider
// names (must exist in SLIDER_META).

interface Props {
  label: string;
  params: { param: string; label: string; max?: number }[];
}

const DISPLAY_NAMES: Record<string, string> = {
  noise_share: "nshare",
  ode_noise: "ode",
  hint_strength: "structure strength",
  dcw_scaler: "DCW low",
  dcw_high_scaler: "DCW high",
};

// Tooltip copy for each tweakable param, surfaced via the slider label's
// hover tooltip in SliderGroup. Concepts here map to the DEMON paper
// (Diffusion Engine for Musical Orchestrated Noise) — paper sections
// are noted in parens where applicable so future readers can find the
// source. Keep these short enough to read in 1–2 seconds; use
// data-dd-tooltip-wide rendering (white-space: normal, max-width 280px).
const PARAM_TOOLTIPS: Record<string, string> = {
  // ── Main remix controls (paper §3.5: denoise is the single most
  //    impactful generation control; §3.6: semantic + timbre hints
  //    feed the multi-condition architecture) ──
  denoise:
    "Remix depth. How much of the model's transformation is applied to the source — 0 keeps the source mostly untouched, 1 fully replaces it. The single most impactful control; takes ~650ms to fully propagate through the ring buffer at depth 8.",
  hint_strength:
    "How strongly the model follows the source's structural cues (sections, rhythm, dynamics, prompt-hint embeddings). Higher = more faithful to the original arrangement; lower = the model invents more freely.",
  timbre_strength:
    "How strongly the source's timbre (instrument character, tone color) bleeds into the output. Independent of structure — keep the rhythm but change the instruments, or vice versa.",

  // ── Engine internals (paper §3.2: per-slot scheduling + variance-
  //    preserving noise interpolation; §3.6: per-frame ODE noise
  //    injection) ──
  feedback:
    "Noise carryover between sequential generations. 0 = each generation independent; 1 = nearly identical noise across generations (variations of each other). 0.3–0.5 provides continuity without collapsing diversity.",
  shift:
    "Timestep-schedule shift. Compresses or expands the denoising trajectory — the turbo model is built around shift=3.0. Lower values spread denoising evenly across steps; higher values concentrate action in early steps.",
  noise_share:
    "Per-frame noise correlation across generations — finer-grained sibling of FEEDBACK. Controls how much initial noise is shared frame-by-frame rather than as one scalar across the whole 60s generation.",
  ode_noise:
    "Stochastic noise injected after each ODE denoising step. Adds controlled creativity to an otherwise deterministic path. Modest values (~0.1) introduce subtle variation; higher values create 'creativity bursts'.",

  // ── DCW (paper §3.4 / Active Research: wavelet-domain post-step
  //    correction; two numeric scalers, plus mode + wavelet choice
  //    selectors elsewhere in the tile) ──
  dcw_scaler:
    "DCW low-band scaler. Wavelet-domain post-step correction targeting low-frequency content (bass, body). Boosts or attenuates the model's low-frequency velocity output before the next step.",
  dcw_high_scaler:
    "DCW high-band scaler. Wavelet-domain post-step correction targeting high-frequency content (transients, brightness, air).",
};

// Per-channel gain tooltips — same concept (paper §6 Active Research:
// Latent channel semantics — empirical characterization of ACE-Step
// 1.5's 64-channel VAE latent space). Generated programmatically so
// the channel index appears in the copy without 14 hand-written
// repetitions.
const CHANNEL_GAINS = ["ch_g0", "ch_g1", "ch_g2", "ch_g3", "ch_g4", "ch_g5", "ch_g6", "ch_g7"] as const;
const NAMED_CHANNELS = ["ch13", "ch14", "ch19", "ch23", "ch29", "ch56"] as const;
for (const [i, p] of CHANNEL_GAINS.entries()) {
  PARAM_TOOLTIPS[p] =
    `Latent channel ${i} gain. Scales channel ${i} of the 64-dim VAE latent before decode. Different channels correlate with different perceptual axes (frequency band, dynamics, transients) — under active characterization.`;
}
for (const p of NAMED_CHANNELS) {
  const idx = p.slice(2);
  PARAM_TOOLTIPS[p] =
    `Latent channel ${idx} scaler. Adjusts the strength of channel ${idx} in the 64-dim VAE latent. Hand-picked channels with empirically characterized perceptual effects (paper §6 Active Research).`;
}

export function tooltipFor(param: string): string | undefined {
  // LoRA strength sliders (param like `lora_str_<id>`) get a generic
  // tooltip rather than per-LoRA copy — the row already carries the
  // LoRA's name as its visible label.
  if (param.startsWith("lora_str_")) {
    return "LoRA strength. Hot-swaps and additively blends this LoRA into the running TensorRT engine (paper §3.4 runtime LoRA refit). Multiple LoRAs stack with independent strengths.";
  }
  if (param === "lora_blend") {
    return "LoRA A / LoRA B crossfade. 0 = LoRA A only, 1 = LoRA B only, 0.5 = both at half strength. Sibling sliders below show the individual strengths.";
  }
  return PARAM_TOOLTIPS[param];
}

// Map slider param → keyboard hint shown beneath the slider. Mirrors the
// chord layout in hooks/useKeyboardShortcuts.ts; if you change one, change
// the other.
const KBD_FOR_PARAM: Record<string, string> = {
  denoise: "A + ▲▼",
  hint_strength: "G + ▲▼",
  timbre_strength: "C + ▲▼",
  feedback: "E + ▲▼",
  shift: "H + ▲▼",
  noise_share: "N + ▲▼",
  ode_noise: "D + ▲▼",
  ch_g0: "0 + ▲▼",
  ch_g1: "1 + ▲▼",
  ch_g2: "2 + ▲▼",
  ch_g3: "3 + ▲▼",
  ch_g4: "4 + ▲▼",
  ch_g5: "5 + ▲▼",
  ch_g6: "6 + ▲▼",
  ch_g7: "7 + ▲▼",
  ch13: "⇧1 + ▲▼",
  ch14: "⇧2 + ▲▼",
  ch19: "⇧3 + ▲▼",
  ch23: "⇧4 + ▲▼",
  ch29: "⇧5 + ▲▼",
  ch56: "⇧6 + ▲▼",
  dcw_scaler: "W + ▲▼",
  dcw_high_scaler: "Y + ▲▼",
};

export function kbdHintFor(param: string): string | undefined {
  return KBD_FOR_PARAM[param];
}

export function SliderTile({ label, params }: Props) {
  return (
    <div className="mixer-tile" data-tile={label.toLowerCase().replace(/ /g, "-")}>
      <div className="mixer-tile-label">{label}</div>
      <div className="mixer-channels">
        {params.map(({ param, label: pLabel, max }) => (
          <SliderGroup
            key={param}
            param={param}
            label={pLabel}
            max={max}
            kbd={KBD_FOR_PARAM[param]}
          />
        ))}
      </div>
    </div>
  );
}

export function defaultLabelFor(param: string): string {
  return DISPLAY_NAMES[param] ?? param.replace(/_/g, " ");
}
