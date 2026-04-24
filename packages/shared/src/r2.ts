// AWS SigV4 presigner for Cloudflare R2 (S3-compat).
// Used only where Workers cannot hand off a native R2 binding —
// most Workers should prefer `env.CLIP_BUCKET.put/get` directly.

export interface R2SignConfig {
  accountId: string;
  accessKeyId: string;
  secretAccessKey: string;
  bucket: string;
}

export async function signR2GetUrl(
  cfg: R2SignConfig,
  key: string,
  expiresSec = 3600,
): Promise<string> {
  const host = `${cfg.accountId}.r2.cloudflarestorage.com`;
  const region = "auto";
  const service = "s3";
  const now = new Date();
  const amzDate = iso8601Basic(now);
  const dateStamp = amzDate.slice(0, 8);

  const credential = `${cfg.accessKeyId}/${dateStamp}/${region}/${service}/aws4_request`;
  const canonicalUri = `/${cfg.bucket}/${encodePath(key)}`;
  const params = new URLSearchParams();
  params.set("X-Amz-Algorithm", "AWS4-HMAC-SHA256");
  params.set("X-Amz-Credential", credential);
  params.set("X-Amz-Date", amzDate);
  params.set("X-Amz-Expires", String(expiresSec));
  params.set("X-Amz-SignedHeaders", "host");
  // URLSearchParams already URL-encodes; S3 wants sorted keys — URLSearchParams preserves insertion order so set them in order.

  const canonicalQuery = [...params.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(
      ([k, v]) => `${rfc3986(k)}=${rfc3986(v)}`,
    )
    .join("&");

  const canonicalHeaders = `host:${host}\n`;
  const signedHeaders = "host";
  const payloadHash = "UNSIGNED-PAYLOAD";

  const canonicalRequest = [
    "GET",
    canonicalUri,
    canonicalQuery,
    canonicalHeaders,
    signedHeaders,
    payloadHash,
  ].join("\n");

  const scope = `${dateStamp}/${region}/${service}/aws4_request`;
  const stringToSign = [
    "AWS4-HMAC-SHA256",
    amzDate,
    scope,
    await sha256Hex(canonicalRequest),
  ].join("\n");

  const signingKey = await deriveSigningKey(
    cfg.secretAccessKey,
    dateStamp,
    region,
    service,
  );
  const signature = toHex(await hmac(signingKey, stringToSign));

  return `https://${host}${canonicalUri}?${canonicalQuery}&X-Amz-Signature=${signature}`;
}

function encodePath(key: string): string {
  return key.split("/").map(rfc3986).join("/");
}

function rfc3986(v: string): string {
  return encodeURIComponent(v).replace(
    /[!'()*]/g,
    (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase(),
  );
}

function iso8601Basic(d: Date): string {
  return d.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
}

async function sha256Hex(msg: string): Promise<string> {
  const buf = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(msg),
  );
  return toHex(new Uint8Array(buf));
}

async function hmac(key: Uint8Array, msg: string): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    toBuffer(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    cryptoKey,
    new TextEncoder().encode(msg),
  );
  return new Uint8Array(sig);
}

async function deriveSigningKey(
  secret: string,
  date: string,
  region: string,
  service: string,
): Promise<Uint8Array> {
  const kDate = await hmac(new TextEncoder().encode("AWS4" + secret), date);
  const kRegion = await hmac(kDate, region);
  const kService = await hmac(kRegion, service);
  return hmac(kService, "aws4_request");
}

function toBuffer(u: Uint8Array): ArrayBuffer {
  // Copy into a fresh ArrayBuffer so SubtleCrypto's overly strict typing is happy.
  const out = new ArrayBuffer(u.byteLength);
  new Uint8Array(out).set(u);
  return out;
}

function toHex(buf: Uint8Array): string {
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}
