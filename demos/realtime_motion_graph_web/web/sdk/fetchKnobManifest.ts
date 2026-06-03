import { fetchWithRetry } from "./fetchWithRetry";
import type { KnobManifest } from "./types/knobs";

/** Fetch the backend knob manifest from `GET /api/knobs`.
 *
 *  The static schema (per-session `lora_str_<id>` knobs and live values
 *  arrive separately over the WS ready frame). `sde` selects the SDE-mode
 *  core set. Uses fetchWithRetry so a still-booting backend (502 from a
 *  dev proxy) is waited on rather than yielding an empty manifest.
 *
 *  `toUrl` maps the API path to a fetchable URL. Default is identity
 *  (same-origin / proxied paths); the shipped app passes its pod URL
 *  builder.
 */
export async function fetchKnobManifest(
  sde = false,
  toUrl: (path: string) => string = (p) => p,
): Promise<KnobManifest> {
  const res = await fetchWithRetry(toUrl(`/api/knobs${sde ? "?sde=1" : ""}`));
  if (!res.ok) throw new Error(`/api/knobs failed: ${res.status}`);
  const json = (await res.json()) as { knobs?: KnobManifest };
  return json.knobs ?? {};
}
