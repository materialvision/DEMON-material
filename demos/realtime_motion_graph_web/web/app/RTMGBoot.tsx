"use client";

import { installDemonDebug } from "@/engine/debugReconnect";
import { fetchKnobManifest } from "@/engine/knobs/fetchKnobManifest";
import { listLoras } from "@/engine/lora/listLoras";
import { setEngineUrlBuilder } from "@/engine/rtmgConfig";
import { installTestHooks } from "@/engine/testHooks";
import { applyConfig, loadConfig } from "@/lib/config";
import { useKnobManifestStore } from "@/store/useKnobManifestStore";
import { useLoraStore } from "@/store/useLoraStore";
import type { KnobManifest } from "@/types/knobs";
import type { LoraCatalogEntry } from "@/types/protocol";

// Same-origin URL builder. The engine's HTTP routes (/api/*, /fixtures/*,
// /loras/*, /videos/*) are proxied to the Python backend at :8765 by the
// Next.js rewrites in next.config.ts. The WebSocket URL goes through
// `defaultWsUrl()` which reads NEXT_PUBLIC_POD_BASE_URL — set in .env.local.
//
// Configured at module load (top-level, not in useEffect) so it's ready
// before any child component's mount-time fetch fires.
setEngineUrlBuilder((path) => (path.startsWith("/") ? path : `/${path}`));

// Fire the config + LoRA catalog fetches in parallel and await both
// before applyConfig(). listLoras' side effect writes the server's
// checkpoint_scale into useSessionStore, which applyConfig reads to
// pick between base (turbo / 2B) and `_xl` variant fields.
//
// After applyConfig we also push the catalog we fetched into the lora
// store. LibraryTile's mount-time listLoras (LOCAL_MODE) races this
// path, and either side could win the /api/loras response. The
// isConfigApplied gate inside setCatalog + the retroactive setCatalog
// trigger inside applyConfig together make the seeding deterministic
// regardless of order — this push just ensures the common case (boot
// wins) does the work directly rather than via the retro path.
if (typeof window !== "undefined") {
  // Expose the WS reconnect test harness on window.__demonDebug. Cheap
  // (no listeners or intervals), so no need to gate on NODE_ENV — having
  // it available in prod also lets us hot-test the reconnect path
  // against a live pod without needing a separate build.
  installDemonDebug();
  // Same posture for the e2e observation hooks on window.__demonTest:
  // installing is just assigning an object of store-reading closures;
  // nothing runs until a test (or a curious operator) calls into it.
  installTestHooks();
  void (async () => {
    const [cfg, catalog, manifest] = await Promise.all([
      loadConfig(),
      listLoras().catch(() => [] as LoraCatalogEntry[]),
      // Non-fatal: the manifest only drives the auto-generated knob panel.
      // A backend without /api/knobs leaves it empty and the panel shows a
      // placeholder; the shipped tiles are unaffected.
      fetchKnobManifest().catch(() => ({}) as KnobManifest),
    ]);
    applyConfig(cfg);
    if (catalog.length > 0) {
      useLoraStore.getState().setCatalog(catalog);
    }
    if (Object.keys(manifest).length > 0) {
      useKnobManifestStore.getState().setManifest(manifest);
    }
  })();
}

export function RTMGBoot() {
  return null;
}
