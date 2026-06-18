// Product-facing terminology — the canonical NOUNS for features/controls that
// the product team renames over time (e.g. Tags → Prompts, LoRAs → Trained
// Styles). Keyed by a STABLE id that never changes; only the label flips, in
// ONE place, so a rename stops meaning "edit the same word across web + VST +
// M4L by hand".
//
// Pure, serializable data (a flat id → label map) and exported from node.ts as
// well as index.ts — so the in-progress native (VST) C++ codegen can emit the
// same labels from this single source instead of hand-authored JUCE strings.
// That is the whole point: web reads TERMS at runtime; the generator reads the
// same TERMS at build time.
//
// Scope: the headline feature/section nouns. Per-knob friendly labels live
// next door in CONTROL_DISPLAY_NAMES (copy.ts); deep inline help prose keeps
// its literals (rewriting whole sentences into term fragments is churn for no
// real rename-saving). Compose compound labels by interpolating a term —
// `${TERMS.lora} Library`, `${TERMS.lora_plural}` — so flipping the noun flows
// through every label that builds on it.

export const TERMS = {
  /** The prompt/tags input feature. */
  tags: "Tags",
  /** A LoRA, singular — for compound labels like `${TERMS.lora} Library`. */
  lora: "LoRA",
  /** LoRAs, plural — for counts, placeholders, empty states. */
  lora_plural: "LoRAs",
} as const;

export type TermId = keyof typeof TERMS;

/** The current display label for a stable term id. */
export function termFor(id: TermId): string {
  return TERMS[id];
}
