// Fetch + merge the operator-defaults file. Parameterized by base URL so a
// non-web host (which may serve config.json from somewhere other than the
// page origin) can point it anywhere; web calls it with no argument, which
// resolves to the page-relative `/config.json` exactly as before.

import { DEFAULT_CONFIG } from "./defaults";
import { mergeConfig } from "./transforms";
import type { RtmgConfig } from "./types";

/** Fetch `<baseUrl>/config.json` (no cache) and merge it onto the bundled
 *  defaults. Missing file or parse error → defaults silently — the bundled
 *  defaults already match the frontends' hardcoded behavior, so a deploy
 *  without a config.json works unchanged. Unknown top-level keys in the
 *  file are preserved through the merge (see `mergeConfig`).
 *
 *  `baseUrl` defaults to "" → the page-relative `/config.json`. Pass an
 *  origin (e.g. "https://pod.example") to fetch from elsewhere. */
export async function loadConfig(baseUrl = ""): Promise<RtmgConfig> {
  try {
    const res = await fetch(`${baseUrl}/config.json?t=${Date.now()}`, {
      cache: "no-store",
    });
    if (!res.ok) return DEFAULT_CONFIG;
    const json = (await res.json()) as Partial<RtmgConfig>;
    return mergeConfig(DEFAULT_CONFIG, json);
  } catch {
    return DEFAULT_CONFIG;
  }
}
