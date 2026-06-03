import { fetchWithRetry } from "@/engine/fetchWithRetry";
import { podHttp } from "@/engine/podUrl";
import type { WireContract } from "@/types/wireContract";

/** Fetch the backend WebSocket wire contract from `GET /api/protocol`.
 *
 *  The self-describing command/event vocabulary (see types/wireContract.ts).
 *  Session-independent — unlike the knob manifest, it doesn't vary with SDE
 *  mode or the enabled LoRAs. Uses fetchWithRetry so a still-booting backend
 *  (502 from the Next dev proxy) is waited on rather than yielding null.
 */
export async function fetchWireContract(): Promise<WireContract | null> {
  const res = await fetchWithRetry(podHttp("/api/protocol"));
  if (!res.ok) throw new Error(`/api/protocol failed: ${res.status}`);
  return (await res.json()) as WireContract;
}
