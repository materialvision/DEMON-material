// The portable inputs codec (@demon/client/inputs) must be byte-identical
// across clients: a clip the web encodes has to decode the same in the M4L
// bridge (Node) and a future C++/VST consumer. These guard the two things
// that would silently diverge — base64 and the 16-bit WAV layout — against
// an independent oracle (Node's Buffer = RFC 4648) and a full round-trip.

import { describe, expect, it } from "vitest";

import {
  arrayBufferToBase64,
  base64ToArrayBuffer,
  encodeWavInterleaved,
} from "@demon/client";

function bytesOf(buf: ArrayBuffer): Uint8Array {
  return new Uint8Array(buf);
}

describe("base64 (environment-neutral)", () => {
  // Cover every residue class mod 3 so the padding logic is exercised.
  const cases = [0, 1, 2, 3, 4, 5, 17, 256, 1023, 65537];

  it("matches Node Buffer base64 exactly (the RFC 4648 oracle)", () => {
    for (const n of cases) {
      const bytes = new Uint8Array(n);
      for (let i = 0; i < n; i++) bytes[i] = (i * 37 + 11) & 0xff;
      const ours = arrayBufferToBase64(bytes.buffer);
      const oracle = Buffer.from(bytes).toString("base64");
      expect(ours, `encode n=${n}`).toBe(oracle);
    }
  });

  it("round-trips arbitrary bytes (decode ∘ encode = identity)", () => {
    for (const n of cases) {
      const bytes = new Uint8Array(n);
      for (let i = 0; i < n; i++) bytes[i] = (i * 101 + 7) & 0xff;
      const back = bytesOf(base64ToArrayBuffer(arrayBufferToBase64(bytes.buffer)));
      expect(back.length, `len n=${n}`).toBe(n);
      expect(Array.from(back), `bytes n=${n}`).toEqual(Array.from(bytes));
    }
  });

  it("decodes a Buffer-produced base64 string (cross-impl decode)", () => {
    const bytes = new Uint8Array(300);
    for (let i = 0; i < bytes.length; i++) bytes[i] = (i * 13) & 0xff;
    const fromNode = Buffer.from(bytes).toString("base64");
    const decoded = bytesOf(base64ToArrayBuffer(fromNode));
    expect(Array.from(decoded)).toEqual(Array.from(bytes));
  });
});

describe("encodeWavInterleaved", () => {
  it("writes a valid 16-bit PCM WAV header", () => {
    const sampleRate = 48000;
    const channels = 2;
    const frames = 100;
    const interleaved = new Float32Array(frames * channels);
    const wav = encodeWavInterleaved(interleaved, channels, sampleRate);
    const view = new DataView(wav);
    const ascii = (o: number, n: number) =>
      String.fromCharCode(...new Uint8Array(wav, o, n));

    expect(ascii(0, 4)).toBe("RIFF");
    expect(ascii(8, 4)).toBe("WAVE");
    expect(ascii(12, 4)).toBe("fmt ");
    expect(view.getUint16(20, true)).toBe(1); // PCM
    expect(view.getUint16(22, true)).toBe(channels);
    expect(view.getUint32(24, true)).toBe(sampleRate);
    expect(view.getUint16(34, true)).toBe(16); // bits/sample
    expect(ascii(36, 4)).toBe("data");
    expect(view.getUint32(40, true)).toBe(frames * channels * 2);
    expect(wav.byteLength).toBe(44 + frames * channels * 2);
  });

  it("quantizes samples to 16-bit and recovers them within tolerance", () => {
    const sampleRate = 44100;
    const channels = 1;
    const src = new Float32Array([0, 0.5, -0.5, 1, -1, 0.25]);
    const wav = encodeWavInterleaved(src, channels, sampleRate);
    const pcm = new Int16Array(wav, 44, src.length);
    // Full-scale and zero are exact; mid values within one 16-bit step.
    expect(pcm[0]).toBe(0);
    expect(pcm[3]).toBe(0x7fff); // +1 → max positive
    expect(pcm[4]).toBe(-0x8000); // -1 → min negative
    for (let i = 0; i < src.length; i++) {
      expect(Math.abs(pcm[i] / 0x8000 - src[i])).toBeLessThan(1 / 32767 + 1e-6);
    }
  });

  it("survives a full clip round-trip: PCM → WAV → base64 → bytes", () => {
    const interleaved = new Float32Array(2000);
    for (let i = 0; i < interleaved.length; i++) {
      interleaved[i] = Math.sin(i / 12) * 0.8;
    }
    const wav = encodeWavInterleaved(interleaved, 2, 48000);
    const b64 = arrayBufferToBase64(wav);
    const back = base64ToArrayBuffer(b64);
    expect(bytesOf(back).length).toBe(bytesOf(wav).length);
    expect(Array.from(bytesOf(back))).toEqual(Array.from(bytesOf(wav)));
  });
});
