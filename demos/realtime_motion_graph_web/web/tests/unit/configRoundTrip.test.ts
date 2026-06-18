// Tier A: the preserve-unknown-on-write guarantee of @demon/client/config.
//
// The load-bearing rule (plan 01): a client reads a config, then writes it
// back, and must re-emit any TOP-LEVEL keys it read but does not model —
// untouched. This is what lets a flat-now / namespaced-later schema be safe,
// and what stops a non-web client's export from silently dropping keys a
// different frontend authored. These tests prove load (merge) → serialize
// and load → capture both round-trip unknown keys byte-for-byte, and that
// the carrier never leaks into a plain JSON.stringify.

import { describe, expect, it } from "vitest";

import {
  captureConfigFromState,
  DEFAULT_CONFIG,
  getUnknownKeys,
  mergeConfig,
  resolveLoraCapForSource,
  selectVariant,
  serializeConfig,
  type ConfigStateSnapshot,
  type RtmgConfig,
} from "@demon/client";

describe("config preserve-unknown on write", () => {
  it("re-emits unknown top-level keys byte-for-byte through merge → serialize", () => {
    const raw = {
      version: 2,
      prompts: { a: "x", b: "y", blend: 0.5 },
      future_section: { foo: 1, bar: ["a", "b"] },
      another_unknown: 42,
    } as unknown as Partial<RtmgConfig>;

    const loaded = mergeConfig(DEFAULT_CONFIG, raw);

    // Known fields still merge as before.
    expect(loaded.prompts.a).toBe("x");
    expect(loaded.prompts.b).toBe("y");
    expect(loaded.version).toBe(2);
    // Untouched known fields keep the bundled default.
    expect(loaded.engine.depth).toBe(DEFAULT_CONFIG.engine.depth);

    // Unknowns are stashed on the carrier...
    expect(getUnknownKeys(loaded)).toEqual({
      future_section: { foo: 1, bar: ["a", "b"] },
      another_unknown: 42,
    });

    // ...re-emitted on an explicit write...
    const out = serializeConfig(loaded);
    expect(out.future_section).toEqual({ foo: 1, bar: ["a", "b"] });
    expect(out.another_unknown).toBe(42);
    expect(out.engine).toBeDefined();

    // ...but the carrier is a non-enumerable symbol, so a plain
    // JSON.stringify of the typed config (web's export path) never leaks it.
    expect(JSON.parse(JSON.stringify(loaded)).future_section).toBeUndefined();
  });

  it("survives the load → capture round trip", () => {
    const raw = {
      my_custom: { nested: true },
    } as unknown as Partial<RtmgConfig>;
    const loaded = mergeConfig(DEFAULT_CONFIG, raw);

    const snapshot: ConfigStateSnapshot = {
      controls: { denoise: 0.5 },
      promptA: "a",
      promptB: "b",
      promptBlend: 0.3,
      key: "G# minor",
      timeSignature: "4",
      seed: 7,
      enabledLoras: [],
      lufsOn: false,
      loop: { band: null, enabled: true, grid: "beat" },
      curves: { scheduleEnabled: false, curves: {} },
    };

    const captured = captureConfigFromState(snapshot, loaded);

    // Unknown keys carried from the loaded config survive the capture.
    expect(getUnknownKeys(captured)).toEqual({ my_custom: { nested: true } });
    expect(serializeConfig(captured).my_custom).toEqual({ nested: true });
    // Live snapshot fields win for the modeled keys.
    expect(captured.controls.denoise).toBe(0.5);
    expect(captured.seed).toBe(7);
    expect(captured.prompts.blend).toBe(0.3);
  });

  it("does not mistake lifted legacy top-level keys for unknowns", () => {
    // Pre-`web` flat shape: effects/audio lived at the top level. The
    // normalize step folds them under `web` — they must NOT be stashed as
    // unknowns (which would duplicate them at the top level on write).
    const raw = {
      effects: {
        parallax_strength: 0.9,
        bloom_on_kick: 0.1,
        bloom_threshold: 0.2,
        warp_strength: 0.1,
      },
    } as unknown as Partial<RtmgConfig>;

    const loaded = mergeConfig(DEFAULT_CONFIG, raw);
    expect(loaded.web.effects.parallax_strength).toBe(0.9);
    expect(getUnknownKeys(loaded)).toEqual({});
    expect(serializeConfig(loaded).effects).toBeUndefined();
  });

  it("carries preserved unknowns through XL (5B) variant resolution", () => {
    const loaded = mergeConfig(DEFAULT_CONFIG, {
      my_unknown: 1,
    } as unknown as Partial<RtmgConfig>);
    expect(getUnknownKeys(selectVariant(loaded, "5B"))).toEqual({
      my_unknown: 1,
    });
    expect(getUnknownKeys(selectVariant(loaded, "2B"))).toEqual({
      my_unknown: 1,
    });
  });
});

describe("config LoRA cap resolver", () => {
  it("treats non-positive static fallback caps as uncapped", () => {
    expect(resolveLoraCapForSource(300, { max_concurrent_loras: 0 })).toBeNull();
    expect(resolveLoraCapForSource(300, { max_concurrent_loras: -1 })).toBeNull();
  });
});
