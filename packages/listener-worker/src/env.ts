export interface Env {
  CHAT_LISTENER: DurableObjectNamespace;
  CLIP_DB: D1Database;
  CLIP_KV: KVNamespace;
  CLIP_INGEST: Queue<unknown>;

  TWITCH_BROADCASTER_ID: string;
  TWITCH_BROADCASTER_LOGIN: string;
  TWITCH_BOT_NICK: string;
  TWITCH_BOT_OAUTH_TOKEN: string;
}
