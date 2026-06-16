// Snap resolutions for the playback loop region ("brace"). The type, the
// coarse→fine order, and the runtime guard now live in @demon/client/config
// (shared with config import/export so the set never drifts). Re-exported
// here so the editor (WaveformScrubBox) and the performance store call sites
// are unchanged. LOOP_GRID_LABEL is UI display text, kept local.

import type { LoopGridRes } from "@demon/client";

export { LOOP_GRID_ORDER, isLoopGridRes } from "@demon/client";
export type { LoopGridRes } from "@demon/client";

export const LOOP_GRID_LABEL: Record<LoopGridRes, string> = {
  bar: "Bar",
  half: "½ Bar",
  beat: "Beat",
  eighth: "⅛",
};
