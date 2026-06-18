"use client";

import { useEffect, useState } from "react";

import { useCurveStore } from "@/store/useCurveStore";
import { useLoraStore } from "@/store/useLoraStore";
import { usePerformanceStore } from "@/store/usePerformanceStore";
import { useSessionStore } from "@/store/useSessionStore";
import { DEFAULT_TIME_SIGNATURE, isTimeSignature } from "@/types/engine";

import {
  applyConfigToState,
  captureConfigFromState,
  DEFAULT_CONFIG,
  isSwapSourceMode,
  resolveLoraCapForSource as resolveLoraCapForSourceCore,
} from "@demon/client";
import type {
  ConfigStateSnapshot,
  RtmgConfig,
  RtmgConfigControls,
  RtmgConfigEngine,
  SwapSourceMode,
} from "@demon/client";

// The operator-defaults config schema, the bundled defaults, and the pure
// transforms / loader / wire mapping now live in @demon/client/config so
// every DEMON frontend shares one implementation. This module keeps only the
// web app's own state wiring (zustand reads/writes, the active-config module
// state, React subscription) and re-exports the portable pieces so existing
// `@/lib/config` call sites are unchanged.
//
// Boot order:
//   1. RTMGBoot (module load) calls loadConfig() → applyConfig().
//   2. applyConfig() pushes values into the relevant zustand stores
//      (perf, lora) and notifies non-store subscribers (effects renderer,
//      PerformanceShell's useConfig()).
//   3. Stores already initialize with hardcoded defaults that match
//      DEFAULT_CONFIG, so first paint is correct even if the fetch is
//      still in flight.

export { DEFAULT_CONFIG, isSwapSourceMode };
export {
  getUnknownKeys,
  loadConfig,
  mergeConfig,
  normalizeConfigShape,
  selectVariant,
  serializeConfig,
} from "@demon/client";
export type {
  EnabledLoraEntry,
  RtmgChannelRange,
  RtmgConfig,
  RtmgConfigAudio,
  RtmgConfigChannelRanges,
  RtmgConfigControls,
  RtmgConfigCurve,
  RtmgConfigCurvePoint,
  RtmgConfigCurves,
  RtmgConfigDenoiseSessionGate,
  RtmgConfigEffects,
  RtmgConfigEngine,
  RtmgConfigLoop,
  RtmgConfigPrompts,
  RtmgWebConfig,
  SwapSourceMode,
} from "@demon/client";

let _activeConfig: RtmgConfig = DEFAULT_CONFIG;
let _configApplied = false;
const listeners = new Set<(c: RtmgConfig) => void>();

/** Snapshot of the active config. Read at point-of-use by code paths
 * that don't need re-render reactivity (e.g. useStartSession.buildConfig
 * runs once per Play click). For reactive reads, use useConfig(). */
export function getConfig(): RtmgConfig {
  return _activeConfig;
}

export function defaultSwapSourceMode(): SwapSourceMode {
  return isSwapSourceMode(_activeConfig.swap_source_mode)
    ? _activeConfig.swap_source_mode
    : DEFAULT_CONFIG.swap_source_mode;
}

/** Resolve the LoRA cap for a given source duration against the active
 *  config's engine block (or an explicit one). Thin wrapper over the SDK's
 *  pure resolver so callers can keep passing just the duration. */
export function resolveLoraCapForSource(
  durationS: number,
  engine: Pick<
    RtmgConfigEngine,
    "max_concurrent_loras" | "max_concurrent_loras_tiers"
  > = _activeConfig.engine,
): number | null {
  return resolveLoraCapForSourceCore(durationS, engine);
}

/** Apply a freshly-resolved cap to the LoRA store AND tell the server
 *  about any LoRAs the cap kicks off the enabled list.
 *
 *  ``setMaxEnabled`` alone is purely a client-store mutation — it
 *  clips ``enabled`` down to the new cap (oldest insertion order
 *  wins, newest are dropped). But the SERVER is unaware of the
 *  clip: those dropped LoRAs stay materialized in GPU memory (~1.2
 *  GiB each), invisible to the user, eating the very budget the
 *  smaller cap was trying to free. ``ghost LoRAs.``
 *
 *  This helper composes the two correctly:
 *   1. Snapshot the current enabled set.
 *   2. Diff against the post-clip view to identify the dropped ids.
 *   3. For each dropped id: ``remote.sendDisableLora(id)`` so the
 *      engine actually frees the refit-state buffer.
 *   4. Re-send the prompt so the trigger prefix drops the now-
 *      disabled LoRAs' triggers (useLoraTriggerSync debounce-sends
 *      automatically when ``enabled`` mutates, but we issue an
 *      immediate send here so the prompt and the disables hit the
 *      server in the same logical step).
 *   5. Finally call ``setMaxEnabled`` to clip the store.
 *
 *  When ``remote`` is null (boot path before any session), skips the
 *  WS sends — no server to notify. The store-side clip still applies. */
export function applyLoraCapWithServerSync(cap: number | null): void {
  const lora = useLoraStore.getState();
  const before = lora.enabled;
  const remote = useSessionStore.getState().remote;

  // Match useLoraStore.clipEnabledToCap semantics: drop the
  // most-recently-added entries (everything past index ``cap``).
  if (remote && typeof cap === "number" && cap >= 0 && before.size > cap) {
    const ids = Array.from(before);
    const toDrop = ids.slice(cap);
    for (const id of toDrop) {
      remote.sendDisableLora(id);
    }
    const perf = usePerformanceStore.getState();
    remote.sendPrompt(
      perf.promptA,
      perf.activeKey,
      perf.activeTimeSignature,
      perf.promptB,
    );
  }

  lora.setMaxEnabled(cap);
}

/** Whether applyConfig() has been called at least once. Once-per-session
 *  seed paths (useLoraStore.setCatalog → computeSeed) gate on this so a
 *  catalog fetch that beats the config fetch doesn't seed against
 *  DEFAULT_CONFIG. LibraryTile fires its own /api/loras in LOCAL_MODE
 *  at mount, racing RTMGBoot's parallel fetches; without this gate the
 *  loser-wins outcome is non-deterministic. */
export function isConfigApplied(): boolean {
  return _configApplied;
}

/** Subscribe to applyConfig() calls. Returns an unsubscribe. Used by
 * non-store consumers (the effects renderer in useRenderLoop) that need
 * to re-apply settings when the config arrives async after their mount. */
export function subscribeConfig(fn: (c: RtmgConfig) => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** React hook variant — subscribes the calling component to config
 * changes so it re-renders when applyConfig() fires. */
export function useConfig(): RtmgConfig {
  const [c, setC] = useState(_activeConfig);
  useEffect(() => subscribeConfig(setC), []);
  return c;
}

/** Lookup the active range for `param`, or null if no override is
 *  configured. Reads from the latest applied config — safe to call
 *  outside React. Consumers that need reactivity should read
 *  `useConfig().channel_ranges` instead. */
export function getChannelRange(param: string) {
  return _activeConfig.channel_ranges[param] ?? null;
}

/** Push the supplied config into stores + non-store subscribers. Idempotent;
 * safe to call multiple times. The only mid-session callers today are the
 * boot path; future "Reload config" affordances would call this too.
 *
 * The pure parts — XL (5B) variant resolution and the deterministic state
 * patch — come from the SDK's `applyConfigToState`; this wrapper owns the
 * zustand writes and the stateful LoRA-diff / engine-send side effects.
 *
 * Resolves the XL variant in-place using the current checkpoint scale —
 * RTMGBoot awaits /api/loras before this runs at boot, so the scale is
 * already known the first time we land here. Mid-session re-applies
 * (Import) read whatever scale is currently set in useSessionStore. */
export function applyConfig(c: RtmgConfig): void {
  const scale = useSessionStore.getState().checkpointScale;
  const patch = applyConfigToState(c, scale);
  const resolved = patch.resolved;
  const firstApply = !_configApplied;
  _activeConfig = resolved;
  _configApplied = true;

  // Numeric controls land on sliderValues + sliderTargets so the slider
  // UI and the param-sync tick agree. prompt_blend rides in here too — it
  // lives in the slider system alongside lora_blend. The non-numeric DCW /
  // RCFG fields are present on the patch only when the config supplied a
  // valid value, so an invalid one leaves the live store field untouched
  // (the same fall-back-to-current semantics this code had inline).
  usePerformanceStore.setState((s) => ({
    sliderDefaults: { ...s.sliderDefaults, ...patch.sliderUpdates },
    sliderValues: { ...s.sliderValues, ...patch.sliderUpdates },
    sliderTargets: { ...s.sliderTargets, ...patch.sliderUpdates },
    promptA: patch.promptA,
    promptB: patch.promptB,
    activeKey: patch.activeKey,
    activeTimeSignature: patch.activeTimeSignature,
    seed: patch.seed,
    lufsOn: patch.lufsOn,
    ...(patch.dcwEnabled !== undefined ? { dcwEnabled: patch.dcwEnabled } : {}),
    ...(patch.dcwMode !== undefined ? { dcwMode: patch.dcwMode } : {}),
    ...(patch.dcwWavelet !== undefined
      ? { dcwWavelet: patch.dcwWavelet }
      : {}),
    ...(patch.rcfgMode !== undefined ? { rcfgMode: patch.rcfgMode } : {}),
  }));

  // Loop region: when the config carried one, push the validated patch into
  // the perf store. WaveformScrubBox subscribes, so a mounted editor
  // re-syncs the worklet + server via its own effect; an import before a
  // session just seeds the store and applies once the waveform mounts.
  if (patch.loop) usePerformanceStore.setState(patch.loop);

  // Curves: when the config carried them, push the whole bag into
  // useCurveStore via setState (the store has no batch action). Skipped when
  // absent — stock pods fall through to the store's own
  // hydratePersistedCurves localStorage path.
  if (patch.curves) useCurveStore.setState(patch.curves);

  // LoRA enable/strength state — stateful + side-effecting, so it stays
  // here rather than in the pure adapter.
  //
  // First applyConfig (boot): if a catalog landed before us (LibraryTile's
  // mount-time /api/loras winning the race against /config.json), the
  // store stashed the catalog but skipped seeding. Re-trigger setCatalog
  // so its once-per-session gate runs against the real enabled_loras.
  //
  // Later applyConfig (an imported config): the store is already seeded,
  // so setCatalog's gate would ignore the new enabled_loras. reset()
  // re-seeds enabled+strengths from the fresh config. The LoRA UI
  // normally sends enable/disable to the engine on click — an import
  // bypasses that path, so push the diff to the engine here and
  // re-encode the prompt so the trigger prefix matches.
  const lora = useLoraStore.getState();
  // Push the boot-time cap. We don't yet know the source duration so
  // resolve against 0 — selects the most-permissive tier. Once a session
  // starts (useStartSession) or a source swap completes (useFixtureSwap),
  // the cap is recomputed against the actual duration. Static
  // ``max_concurrent_loras`` (no tiers) is duration-independent so the boot
  // value persists.
  lora.setMaxEnabled(resolveLoraCapForSource(0, resolved.engine));
  if (firstApply) {
    if (!lora.seeded && lora.catalog.length > 0) {
      lora.setCatalog(lora.catalog);
    }
  } else if (lora.catalog.length > 0) {
    const before = new Set(lora.enabled);
    // setMaxEnabled above already re-clipped any over-cap entries from
    // the prior session; reset() now re-seeds against the new config.
    lora.reset();
    const after = useLoraStore.getState();
    const remote = useSessionStore.getState().remote;
    if (remote) {
      for (const id of before) {
        if (!after.enabled.has(id)) remote.sendDisableLora(id);
      }
      for (const id of after.enabled) {
        if (!before.has(id)) {
          remote.sendEnableLora(id, after.strengths[id] ?? 0);
        }
      }
      remote.sendPrompt(
        resolved.prompts.a,
        resolved.engine.key,
        isTimeSignature(resolved.engine.time_signature)
          ? resolved.engine.time_signature
          : DEFAULT_TIME_SIGNATURE,
        resolved.prompts.b,
      );
    }
  }

  for (const fn of listeners) fn(resolved);
}

/**
 * Snapshot the live stores into an `RtmgConfig` — the inverse of
 * `applyConfig`. Used by the OperatorStrip's Export button and by any
 * caller (demon-public-demo's `captureSessionState`) that wants the
 * DEMON-shaped base of a session without rebuilding the field-mapping
 * logic. Gathers the neutral snapshot from the stores and hands it to the
 * SDK's pure `captureConfigFromState`, which assembles the config and
 * re-emits any preserved-unknown top-level keys.
 *
 * Fields the stores don't own (channel_ranges, swap_source_mode, the
 * non-numeric engine.* config, and the whole `web` block) are pulled from
 * the active config so exports round-trip cleanly through Import.
 */
export function captureRtmgConfig(): RtmgConfig {
  const perf = usePerformanceStore.getState();
  const lora = useLoraStore.getState();
  const curveStore = useCurveStore.getState();
  const active = _activeConfig;

  // Numeric controls land on sliderTargets in the perf store. The DCW
  // non-numeric controls live on dedicated store fields.
  const controls: RtmgConfigControls = { ...perf.sliderTargets };
  controls.dcw_enabled = perf.dcwEnabled;
  controls.dcw_mode = perf.dcwMode;
  controls.dcw_wavelet = perf.dcwWavelet;
  // lora_default_strength isn't tracked live in the perf store; pull
  // from active config so the export reflects the seed value.
  if (typeof active.controls.lora_default_strength !== "undefined") {
    controls.lora_default_strength = active.controls.lora_default_strength;
  }

  const snapshot: ConfigStateSnapshot = {
    controls,
    promptA: perf.promptA,
    promptB: perf.promptB,
    promptBlend: perf.sliderTargets.prompt_blend ?? 0,
    key: perf.activeKey,
    timeSignature: perf.activeTimeSignature ?? active.engine.time_signature,
    seed: perf.seed,
    enabledLoras: Array.from(lora.enabled).map((id) => {
      const strength = lora.strengths[id];
      return typeof strength === "number" ? { name: id, strength } : id;
    }),
    lufsOn: perf.lufsOn,
    loop: {
      band: perf.loopBand,
      enabled: perf.bandLoopEnabled,
      grid: perf.loopGridRes,
      fullBuffer: perf.loopOn,
    },
    curves: {
      scheduleEnabled: curveStore.scheduleEnabled,
      curves: Object.fromEntries(
        Object.entries(curveStore.curves).map(([param, curve]) => [
          param,
          {
            enabled: curve.enabled,
            points: curve.points.map((p) => ({ x: p.x, y: p.y, mode: p.mode })),
          },
        ]),
      ),
    },
  };

  return captureConfigFromState(snapshot, active);
}
