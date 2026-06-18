"use client";

import { SliderGroup } from "./SliderGroup";

// Generic mixer tile that wraps a row of sliders. Replaces the dynamic
// buildChannelTile() helper from app.js. `params` is the list of slider
// names (must exist in SLIDER_META).

interface Props {
  label: string;
  params: { param: string; label: string; max?: number }[];
}

// User-facing control copy (tooltips) + display names now live in the SDK at
// @demon/client/controls — the portable, hand-authored editorial layer,
// distinct from the terse agent-facing descriptions on /api/knobs. They are
// re-exported here under the web's historical names so existing call sites
// (SliderGroup, Knob, CoreTile, ModTile, VoiceTile, GraphLaneLabels, …) stay
// unchanged. To surface a manifest fallback for runtime-only knobs, use the
// SDK's resolveControlDescription directly.
export { describeControl as tooltipFor, displayNameFor as defaultLabelFor } from "@demon/client";

// Map slider param → keyboard hint shown beneath the slider. WEB-ONLY: these
// are this demo's keyboard chords, not portable control semantics, so they
// stay here rather than in the SDK. Mirrors the chord layout in
// hooks/useKeyboardShortcuts.ts; if you change one, change the other.
const KBD_FOR_PARAM: Record<string, string> = {
  denoise: "A + ▲▼",
  hint_strength: "G + ▲▼",
  timbre_strength: "C + ▲▼",
  feedback: "E + ▲▼",
  feedback_depth: "D + ▲▼",
  shift: "H + ▲▼",
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
