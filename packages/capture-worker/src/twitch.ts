// Minimal Twitch Helix client — just the Clips endpoints we need.

const HELIX = "https://api.twitch.tv/helix";
const GQL = "https://gql.twitch.tv/gql";
const CLIP_ACCESS_QUERY_HASH = "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4c35796eb11";
const GQL_PUBLIC_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko";

/** Thrown for HTTP 4xx errors that will never succeed on retry. */
export class PermanentTwitchError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "PermanentTwitchError";
  }
}

export interface CreatedClip {
  id: string;
  edit_url: string;
}

export interface HelixClip {
  id: string;
  url: string;
  embed_url: string;
  broadcaster_id: string;
  broadcaster_name: string;
  creator_id: string;
  creator_name: string;
  video_id: string;
  game_id: string;
  language: string;
  title: string;
  view_count: number;
  created_at: string;
  thumbnail_url: string;
  duration: number;
  vod_offset: number | null;
}

interface GqlClipQuality {
  frameRate?: number;
  quality?: string;
  sourceURL?: string;
}

interface GqlClipPlaybackToken {
  signature?: string;
  value?: string;
}

interface GqlClipPlaybackResponse {
  data?: {
    clip?: {
      playbackAccessToken?: GqlClipPlaybackToken;
      videoQualities?: GqlClipQuality[];
    } | null;
  };
}

export async function createClip(
  clientId: string,
  broadcasterToken: string, // clips:edit scope
  broadcasterId: string,
): Promise<CreatedClip> {
  const res = await fetch(
    `${HELIX}/clips?broadcaster_id=${encodeURIComponent(broadcasterId)}&has_delay=false`,
    {
      method: "POST",
      headers: {
        "Client-ID": clientId,
        Authorization: `Bearer ${stripBearerPrefix(broadcasterToken)}`,
      },
    },
  );
  const text = await res.text();
  if (!res.ok) {
    // 4xx errors (bad request, forbidden, etc.) are permanent — retrying won't help.
    if (res.status >= 400 && res.status < 500) {
      throw new PermanentTwitchError(res.status, `createClip ${res.status}: ${text}`);
    }
    throw new Error(`createClip ${res.status}: ${text}`);
  }
  const body = JSON.parse(text) as { data: CreatedClip[] };
  const created = body.data[0];
  if (!created) throw new Error(`createClip empty data: ${text}`);
  return created;
}

export async function getClipById(
  clientId: string,
  appTokenOrBroadcaster: string,
  clipId: string,
): Promise<HelixClip | null> {
  const res = await fetch(`${HELIX}/clips?id=${encodeURIComponent(clipId)}`, {
    headers: {
      "Client-ID": clientId,
      Authorization: `Bearer ${stripBearerPrefix(appTokenOrBroadcaster)}`,
    },
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`getClip ${res.status}: ${text}`);
  const body = JSON.parse(text) as { data: HelixClip[] };
  return body.data[0] ?? null;
}

export async function getClipPlaybackUrl(
  clientId: string,
  clipSlug: string,
): Promise<string | null> {
  const candidates = [clientId, GQL_PUBLIC_CLIENT_ID].filter(
    (id, i, arr): id is string => Boolean(id) && arr.indexOf(id) === i,
  );

  let body: GqlClipPlaybackResponse | null = null;
  let lastErr: string | null = null;
  for (let i = 0; i < candidates.length; i += 1) {
    const gqlClientId = candidates[i]!;
    const res = await fetch(GQL, {
      method: "POST",
      headers: {
        "Client-ID": gqlClientId,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        operationName: "VideoAccessToken_Clip",
        variables: { slug: clipSlug },
        extensions: {
          persistedQuery: {
            version: 1,
            sha256Hash: CLIP_ACCESS_QUERY_HASH,
          },
        },
      }),
    });

    const text = await res.text();
    if (!res.ok) {
      lastErr = `client ${gqlClientId}: ${res.status}: ${text}`;
      // Try all candidate client IDs before failing.
      if (i < candidates.length - 1) {
        continue;
      }
      throw new Error(`getClipPlaybackUrl ${lastErr}`);
    }
    body = JSON.parse(text) as GqlClipPlaybackResponse;
    break;
  }

  if (!body) {
    throw new Error(`getClipPlaybackUrl failed: ${lastErr ?? "no response"}`);
  }

  const clip = body.data?.clip;
  if (!clip) return null;

  const videoQualities = [...(clip.videoQualities ?? [])]
    .filter((vq) => Boolean(vq.sourceURL))
    .sort((a, b) => {
      const aq = Number.parseInt(a.quality ?? "", 10);
      const bq = Number.parseInt(b.quality ?? "", 10);
      if (!Number.isNaN(aq) && !Number.isNaN(bq) && aq !== bq) {
        return bq - aq;
      }
      if (!Number.isNaN(aq) && Number.isNaN(bq)) return -1;
      if (Number.isNaN(aq) && !Number.isNaN(bq)) return 1;
      return (b.frameRate ?? 0) - (a.frameRate ?? 0);
    });

  const best = videoQualities[0];
  const sig = clip.playbackAccessToken?.signature;
  const token = clip.playbackAccessToken?.value;
  if (best?.sourceURL) {
    return sig && token ? withPlaybackAuth(best.sourceURL, sig, token) : best.sourceURL;
  }

  if (!sig || !token) return null;

  try {
    const parsed = JSON.parse(token) as { clip_uri?: string };
    if (!parsed.clip_uri) return null;
    return withPlaybackAuth(parsed.clip_uri, sig, token);
  } catch {
    return null;
  }
}

function withPlaybackAuth(url: string, sig: string, token: string): string {
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}sig=${encodeURIComponent(sig)}&token=${encodeURIComponent(token)}`;
}

/**
 * Convert the clip's CDN thumbnail_url to the underlying MP4 URL.
 * Twitch thumbs look like:
 *   https://clips-media-assets2.twitch.tv/<slug>-preview-480x272.jpg
 * The corresponding MP4 lives at:
 *   https://clips-media-assets2.twitch.tv/<slug>.mp4
 */
export function mp4UrlFromThumbnail(thumbnailUrl: string): string {
  const url = new URL(thumbnailUrl);
  const path = url.pathname
    // Common Twitch thumbnail forms.
    .replace(/-preview-\d+x\d+\.jpg$/i, ".mp4")
    .replace(/-social-preview\.jpg$/i, ".mp4");

  if (path === url.pathname) {
    throw new Error(`unexpected thumbnail format: ${thumbnailUrl}`);
  }

  url.pathname = path;
  url.search = "";
  url.hash = "";
  return url.toString();
}

function stripBearerPrefix(tok: string): string {
  return tok.replace(/^oauth:/i, "").replace(/^bearer\s+/i, "");
}
