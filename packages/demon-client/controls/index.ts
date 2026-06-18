// Portable control copy — the user-facing descriptions and display names a
// DEMON frontend renders on its knobs/inputs. Hand-authored and client-side,
// distinct from the backend-served /api/knobs manifest (whose `description` is
// the terse, agent-facing layer). See ./copy.ts for the data and the audience
// split; `resolveControlDescription` below is the merge point between the two.

import type { KnobManifest } from "../types/knobs";
import {
  CONTROL_DESCRIPTIONS,
  CONTROL_DISPLAY_NAMES,
  LORA_STRENGTH_DESCRIPTION,
  LORA_BLEND_DESCRIPTION,
  MANUAL_SRC_DESCRIPTION,
  MANUAL_LAYER_DESCRIPTION,
  MANUAL_STEP_DESCRIPTION,
  MANUAL_ALPHA_DESCRIPTION,
} from "./copy";

export * from "./copy";

/** User-facing display label for a param id. Friendly overrides win; the
 *  fallback turns engine snake_case into spaced words so the UI still matches
 *  what the engine, MIDI map, and config files call it. */
export function displayNameFor(param: string): string {
  return CONTROL_DISPLAY_NAMES[param] ?? param.replace(/_/g, " ");
}

/** Rich user-facing description for a param id, or undefined if none is
 *  authored. The runtime-generated knob families (LoRA strength sliders,
 *  manual-steering slots) share one string each and are matched by prefix —
 *  the visible row label already carries their per-instance name. */
export function describeControl(param: string): string | undefined {
  if (param.startsWith("lora_str_")) return LORA_STRENGTH_DESCRIPTION;
  if (param === "lora_blend") return LORA_BLEND_DESCRIPTION;
  if (param.startsWith("man_src_")) return MANUAL_SRC_DESCRIPTION;
  if (param.startsWith("man_layer_")) return MANUAL_LAYER_DESCRIPTION;
  if (param.startsWith("man_step_")) return MANUAL_STEP_DESCRIPTION;
  if (param.startsWith("man_alpha_")) return MANUAL_ALPHA_DESCRIPTION;
  return CONTROL_DESCRIPTIONS[param];
}

/** One lookup that prefers the rich editorial copy and falls back to the live
 *  knob manifest's terse description — so knobs that only exist at runtime
 *  (and thus have no hand-authored copy) still surface *something*. Pass the
 *  `/api/knobs` manifest (from fetchKnobManifest) as the second arg. */
export function resolveControlDescription(
  param: string,
  manifest?: KnobManifest,
): string | undefined {
  return describeControl(param) ?? manifest?.[param]?.description;
}
