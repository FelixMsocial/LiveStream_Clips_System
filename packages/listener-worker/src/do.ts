// ChatListenerDO — single Durable Object instance that owns one Twitch IRC WebSocket.
// On any disconnect it backs off (1s → 2s → 4s → 8s → 30s cap) and reconnects.
// Each !clip from a whitelisted mod → D1 row + CLIP_INGEST queue message.

import { clipsDb, uuidv7, type ClipIngestJob } from "@clipfactory/shared";
import type { Env } from "./env.js";

// ---------------------------------------------------------------------------
// Retry helper — retries transient D1 / queue errors with exponential backoff.
// Only D1_ERROR / timeout messages are considered transient; everything else
// propagates immediately on the first attempt.
// ---------------------------------------------------------------------------
async function withRetry<T>(
  fn: () => Promise<T>,
  opts: { attempts?: number; label?: string } = {},
): Promise<T> {
  const maxAttempts = opts.attempts ?? 3;
  const delays = [100, 300, 900]; // ms
  let lastErr: unknown;
  for (let i = 0; i < maxAttempts; i++) {
    try {
      return await fn();
    } catch (e) {
      lastErr = e;
      const msg = String(e);
      const isTransient = msg.includes("D1_ERROR") || msg.includes("timeout");
      if (!isTransient || i === maxAttempts - 1) throw e;
      const delay = delays[i] ?? 900;
      console.warn(
        `[retry ${i + 1}/${maxAttempts}] ${opts.label ?? "op"} failed, retrying in ${delay}ms:`,
        msg,
      );
      await new Promise((r) => setTimeout(r, delay));
    }
  }
  throw lastErr;
}

const WHITELIST_CACHE_KEY = "whitelist:cache";
const WHITELIST_CACHE_TTL = 60; // seconds
const IRC_WSS = "wss://irc-ws.chat.twitch.tv:443";
const RECONNECT_MAX_MS = 30_000;
const PING_INTERVAL_MS = 30_000;
const STALE_AFTER_MS = 120_000; // if we haven't seen anything for 2m, force reconnect.

interface Status {
  connected: boolean;
  last_message_at: number | null;
  last_error: string | null;
  reconnect_attempts: number;
  started_at: number | null;
}

export class ChatListenerDO {
  private ws: WebSocket | null = null;
  private status: Status = {
    connected: false,
    last_message_at: null,
    last_error: null,
    reconnect_attempts: 0,
    started_at: null,
  };
  private reconnectTimer: number | null = null;
  private pingTimer: number | null = null;
  private whitelist: Set<string> | null = null;
  private whitelistAt = 0;
  private currentSessionId: string | null = null;
  private disconnectedAt: number | null = null;

  constructor(
    private readonly state: DurableObjectState,
    private readonly env: Env,
  ) {
    this.state.blockConcurrencyWhile(async () => {
      // Auto-start the connection as soon as the DO is instantiated.
      await this.connect();
    });
  }

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    switch (url.pathname) {
      case "/status":
        return Response.json(this.status);
      case "/start":
        await this.connect();
        return new Response("ok");
      case "/heartbeat":
        await this.heartbeat();
        return new Response("ok");
      default:
        return new Response("not found", { status: 404 });
    }
  }

  private async heartbeat(): Promise<void> {
    await withRetry(
      () =>
        this.env.CLIP_DB.prepare(
          `INSERT INTO worker_heartbeats (worker_id, last_seen_ts, meta, updated_at)
           VALUES ('listener', ?1, NULL, datetime('now'))
           ON CONFLICT(worker_id) DO UPDATE SET
             last_seen_ts = excluded.last_seen_ts,
             updated_at   = datetime('now')`,
        ).bind(Date.now()).run(),
      { label: "listener heartbeat upsert" },
    );
    const stale =
      this.status.last_message_at !== null &&
      Date.now() - this.status.last_message_at > STALE_AFTER_MS;
    if (!this.status.connected || stale) {
      console.warn(
        `listener stale (connected=${this.status.connected}, stale=${stale}) — reconnecting`,
      );
      this.scheduleReconnect(0);
    }
  }

  private async connect(): Promise<void> {
    this.teardown();
    const { TWITCH_BOT_NICK, TWITCH_BOT_OAUTH_TOKEN, TWITCH_BROADCASTER_LOGIN } = this.env;
    if (!TWITCH_BOT_NICK || !TWITCH_BOT_OAUTH_TOKEN || !TWITCH_BROADCASTER_LOGIN) {
      this.status.last_error = "missing TWITCH_BOT_NICK / OAUTH_TOKEN / BROADCASTER_LOGIN";
      return;
    }

    // Workers open outbound WebSockets via fetch() Upgrade (no `new WebSocket()`).
    let upgrade: Response;
    try {
      upgrade = await fetch(IRC_WSS.replace(/^wss:/, "https:"), {
        headers: { Upgrade: "websocket" },
      });
    } catch (e) {
      this.status.last_error = `ws fetch threw: ${String(e)}`;
      this.scheduleReconnect();
      return;
    }
    const ws = upgrade.webSocket;
    if (!ws) {
      this.status.last_error = `ws upgrade failed: ${upgrade.status}`;
      this.scheduleReconnect();
      return;
    }
    ws.accept();
    this.ws = ws;
    this.status.started_at = Date.now();

    // IRC login. Twitch requires CAP REQ before NICK/PASS for tags + commands.
    ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands");
    ws.send(`PASS ${TWITCH_BOT_OAUTH_TOKEN}`);
    ws.send(`NICK ${TWITCH_BOT_NICK.toLowerCase()}`);
    ws.send(`JOIN #${TWITCH_BROADCASTER_LOGIN.toLowerCase()}`);
    this.status.connected = true;
    this.status.last_error = null;
    this.status.reconnect_attempts = 0;

    // Log disconnect gap if > 5 seconds — potential missed !clip commands.
    if (this.disconnectedAt !== null) {
      const gapMs = Date.now() - this.disconnectedAt;
      if (gapMs > 5000) {
        const gapSec = Math.round(gapMs / 1000);
        const disconnectIso = new Date(this.disconnectedAt).toISOString();
        const reconnectIso = new Date().toISOString();
        withRetry(
          () => clipsDb.insertClip(this.env.CLIP_DB, {
            id: uuidv7(),
            triggered_by: "system",
            triggered_at: disconnectIso,
            label: `IRC disconnect gap: ${gapSec}s (${disconnectIso} → ${reconnectIso})`,
            status: "missed",
            stream_session_id: this.currentSessionId,
          }),
          { label: "insertClip:missed" },
        ).catch((e) => console.error("missed status insert failed", e));
      }
      this.disconnectedAt = null;
    }
    this.pingTimer = setInterval(() => {
      try {
        ws.send("PING :tmi.twitch.tv");
      } catch (e) {
        console.error("ping failed", e);
      }
    }, PING_INTERVAL_MS) as unknown as number;

    ws.addEventListener("message", (ev: MessageEvent) => {
      const data = typeof ev.data === "string" ? ev.data : "";
      this.status.last_message_at = Date.now();
      for (const rawLine of data.split("\r\n")) {
        const line = rawLine.trim();
        if (!line) continue;
        this.handleLine(line).catch((e) => console.error("handleLine error", e));
      }
    });

    ws.addEventListener("close", (ev: CloseEvent) => {
      this.status.connected = false;
      this.status.last_error = `close ${ev.code} ${ev.reason ?? ""}`;
      if (this.disconnectedAt === null) this.disconnectedAt = Date.now();
      this.scheduleReconnect();
    });

    ws.addEventListener("error", (ev: Event) => {
      this.status.last_error = `ws error ${String((ev as ErrorEvent).message ?? "")}`;
      if (this.disconnectedAt === null) this.disconnectedAt = Date.now();
    });
  }

  private teardown(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
    this.status.connected = false;
  }

  private scheduleReconnect(delayMs?: number): void {
    if (this.reconnectTimer !== null) return;
    this.status.reconnect_attempts += 1;
    const backoff =
      delayMs ??
      Math.min(RECONNECT_MAX_MS, 1000 * 2 ** Math.min(this.status.reconnect_attempts, 5));
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect().catch((e) => {
        this.status.last_error = `reconnect throw: ${String(e)}`;
        this.scheduleReconnect();
      });
    }, backoff) as unknown as number;
  }

  private async handleLine(line: string): Promise<void> {
    // Keepalive
    if (line.startsWith("PING ")) {
      this.ws?.send(line.replace("PING", "PONG"));
      return;
    }

    const parsed = parseIrc(line);
    if (!parsed || parsed.command !== "PRIVMSG") return;

    const message = parsed.trailing ?? "";
    const match = /^!clip(?:\s+(.+))?$/i.exec(message.trim());
    if (!match) return;

    const login = parsed.sourceLogin ?? "";
    if (!login) return;

    const whitelisted = await this.isWhitelisted(login);
    const label = (match[1] ?? "").trim() || null;
    const clipId = uuidv7();
    const triggeredAt = new Date().toISOString();

    // Lazily generate a date-based session ID (one per calendar day).
    const todaySession = `session-${new Date().toISOString().slice(0, 10)}`;
    if (this.currentSessionId !== todaySession) {
      this.currentSessionId = todaySession;
    }

    if (!whitelisted) {
      // Still record for visibility, but do not enqueue.
      await withRetry(
        () => clipsDb.insertClip(this.env.CLIP_DB, {
          id: clipId,
          triggered_by: login,
          triggered_at: triggeredAt,
          label,
          status: "ignored",
          stream_session_id: this.currentSessionId,
        }),
        { label: "insertClip:ignored" },
      );
      return;
    }

    // Critical path — if any step here fails after all retries, log a structured
    // drop record so operators can identify and manually recover the clip.
    try {
      await withRetry(
        () => clipsDb.insertClip(this.env.CLIP_DB, {
          id: clipId,
          triggered_by: login,
          triggered_at: triggeredAt,
          label,
          status: "raw",
          stream_session_id: this.currentSessionId,
        }),
        { label: "insertClip:raw" },
      );
      await withRetry(
        () => clipsDb.appendApprovalLog(
          this.env.CLIP_DB,
          clipId,
          "triggered",
          login,
          { label, via: "irc" },
        ),
        { label: "appendApprovalLog:triggered" },
      );

      const job: ClipIngestJob = {
        clip_id: clipId,
        broadcaster_id: this.env.TWITCH_BROADCASTER_ID,
        broadcaster_login: this.env.TWITCH_BROADCASTER_LOGIN,
        triggered_by: login,
        triggered_at: triggeredAt,
        label: label ?? undefined,
        stream_session_id: this.currentSessionId ?? undefined,
      };
      await withRetry(
        () => this.env.CLIP_INGEST.send(job),
        { label: "queue:send" },
      );
    } catch (e) {
      // All retries exhausted — emit a structured drop record so this clip
      // can be recovered manually from logs.
      console.error(
        `[clip-drop] clip_id=${clipId} triggered_by=${login} triggered_at=${triggeredAt} label=${label ?? "(none)"} error=${String(e)}`,
      );
      throw e;
    }
  }

  private async isWhitelisted(login: string): Promise<boolean> {
    const key = login.toLowerCase();
    const now = Date.now();
    if (!this.whitelist || now - this.whitelistAt > WHITELIST_CACHE_TTL * 1000) {
      // Prefer KV-cached whitelist across DO restarts.
      const fromKv = await this.env.CLIP_KV.get(WHITELIST_CACHE_KEY);
      if (fromKv) {
        this.whitelist = new Set(JSON.parse(fromKv) as string[]);
        this.whitelistAt = now;
      } else {
        const res = await withRetry(
          () => this.env.CLIP_DB.prepare(
            `SELECT twitch_username FROM mod_whitelist WHERE active = 1`,
          ).all<{ twitch_username: string }>(),
          { label: "whitelist:db" },
        );
        const list = (res.results ?? []).map((r) => r.twitch_username.toLowerCase());
        this.whitelist = new Set(list);
        this.whitelistAt = now;
        await this.env.CLIP_KV.put(WHITELIST_CACHE_KEY, JSON.stringify(list), {
          expirationTtl: WHITELIST_CACHE_TTL,
        });
      }
    }
    return this.whitelist.has(key);
  }
}

interface IrcLine {
  tags: Record<string, string>;
  sourceLogin: string | null;
  command: string;
  params: string[];
  trailing: string | null;
}

function parseIrc(line: string): IrcLine | null {
  let rest = line;
  const tags: Record<string, string> = {};
  if (rest.startsWith("@")) {
    const sp = rest.indexOf(" ");
    if (sp === -1) return null;
    const tagStr = rest.slice(1, sp);
    rest = rest.slice(sp + 1);
    for (const t of tagStr.split(";")) {
      const eq = t.indexOf("=");
      if (eq === -1) tags[t] = "";
      else tags[t.slice(0, eq)] = t.slice(eq + 1);
    }
  }
  let sourceLogin: string | null = null;
  if (rest.startsWith(":")) {
    const sp = rest.indexOf(" ");
    if (sp === -1) return null;
    const source = rest.slice(1, sp);
    rest = rest.slice(sp + 1);
    const bang = source.indexOf("!");
    sourceLogin = bang === -1 ? source.split("@")[0]! : source.slice(0, bang);
    sourceLogin = sourceLogin.toLowerCase();
  }
  // Trailing param after " :".
  let trailing: string | null = null;
  const trailIdx = rest.indexOf(" :");
  if (trailIdx !== -1) {
    trailing = rest.slice(trailIdx + 2);
    rest = rest.slice(0, trailIdx);
  }
  const parts = rest.split(" ").filter(Boolean);
  if (parts.length === 0) return null;
  const command = parts[0]!;
  const params = parts.slice(1);
  return { tags, sourceLogin, command, params, trailing };
}
