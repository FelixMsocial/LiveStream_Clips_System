// UUIDv7 — time-ordered, sortable, 16 bytes.
// Implementation is intentionally dependency-free for Workers + DO compatibility.

export function uuidv7(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);

  const ms = BigInt(Date.now());
  // Top 48 bits: unix_ts_ms (big-endian).
  bytes[0] = Number((ms >> 40n) & 0xffn);
  bytes[1] = Number((ms >> 32n) & 0xffn);
  bytes[2] = Number((ms >> 24n) & 0xffn);
  bytes[3] = Number((ms >> 16n) & 0xffn);
  bytes[4] = Number((ms >> 8n) & 0xffn);
  bytes[5] = Number(ms & 0xffn);

  // Version 7 in high nibble of byte 6.
  bytes[6] = (bytes[6]! & 0x0f) | 0x70;
  // Variant 10 in top 2 bits of byte 8.
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;

  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(
    16,
    20,
  )}-${hex.slice(20)}`;
}
