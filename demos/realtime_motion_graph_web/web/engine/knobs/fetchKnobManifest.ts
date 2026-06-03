import { fetchWithRetry } from "@/engine/fetchWithRetry";
import { podHttp } from "@/engine/podUrl";
import type { KnobManifest } from "@/types/knobs";

/** Fetch the backend knob manifest from `GET /api/knobs`.
 *
 *  The static schema (per-session `lora_str_<id>` knobs and live values
 *  arrive separately over the WS ready frame). `sde` selects the SDE-mode
 *  core set. Uses fetchWithRetry so a still-booting backend (502 from the
 *  Next dev proxy) is waited on rather than yielding an empty manifest.
 */
export async function fetchKnobManifest(sde = false): Promise<KnobManifest> {
  const res = await fetchWithRetry(podHttp(`/api/knobs${sde ? "?sde=1" : ""}`));
  if (!res.ok) throw new Error(`/api/knobs failed: ${res.status}`);
  const json = (await res.json()) as { knobs?: KnobManifest };
  return json.knobs ?? {};
}
