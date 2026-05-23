// ChatListenerDO — single Durable Object instance that owns one Twitch IRC WebSocket.
// Uses the Alarm API for reconnects so they fire even after DO eviction. setTimeout
// and setInterval do not survive eviction — alarms do.
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

// Watchdog alarm interval while connected — ensures the DO is woken at least
// this often so it can respond to Twitch PINGs and detect stale connections.
const WATCHDOG_INTERVAL_MS = 25_000;

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
  private pingTimer: number | null = null;
  private whitelist: Set<string> | null = null;
  private whitelistAt = 0;
  private currentSessionId: string | null = null;
  private disconnectedAt: number | null = null;
  private sawPrivmsg = false;

  constructor(
    private readonly state: DurableObjectState,
    private readonly env: Env,
  ) {
    this.state.blockConcurrencyWhile(async () => {
      // Restore persisted state across eviction cycles.
      const stored = await this.state.storage.get<Status>("status");
      if (stored) this.status = stored;
      const storedGap = await this.state.storage.get<number>("disconnectedAt");
      if (storedGap != null) this.disconnectedAt = storedGap;

      // Auto-connect on instantiation.
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

  // ---------------------------------------------------------------------------
  // Alarm API — fires reliably even after DO eviction, unlike setTimeout.
  // Used as a watchdog: reconnects if the WS died while the DO was asleep.
  // ---------------------------------------------------------------------------

  async alarm(): Promise<void> {
    if (this.ws === null) {
      // WS was lost while the DO was evicted — reconnect.
      await this.connect();
    } else {
      // Still alive — check for stale connection and reschedule watchdog.
      const stale =
        this.status.last_message_at !== null &&
        Date.now() - this.status.last_message_at > STALE_AFTER_MS;
      if (stale) {
        console.warn("listener stale in alarm — forcing reconnect");
        await this.connect();
      }
    }
    // Keep the watchdog ticking while connected.
    if (this.ws !== null) {
      await this.state.storage.setAlarm(Date.now() + WATCHDOG_INTERVAL_MS);
    }
  }

  // ---------------------------------------------------------------------------
  // Internal helpers
  // ---------------------------------------------------------------------------

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
      await this.connect();
    }
  }

  private async connect(): Promise<void> {
    // If a live WS already exists, skip — alarm() guards against double-connect
    // on fresh instantiation after the constructor also called connect().
    if (this.ws !== null) return;

    this.teardown();

    const { TWITCH_BOT_NICK, TWITCH_BOT_OAUTH_TOKEN, TWITCH_BROADCASTER_LOGIN } = this.env;
    if (!TWITCH_BOT_NICK || !TWITCH_BOT_OAUTH_TOKEN || !TWITCH_BROADCASTER_LOGIN) {
      this.status.last_error = "missing TWITCH_BOT_NICK / OAUTH_TOKEN / BROADCASTER_LOGIN";
      console.error("[irc] missing required env vars for connection");
      await this.persistStatus();
      return;
    }

    console.log(
      `[irc] connecting to #${TWITCH_BROADCASTER_LOGIN.toLowerCase()} as ${TWITCH_BOT_NICK.toLowerCase()}`,
    );

    // Workers open outbound WebSockets via fetch() Upgrade (no `new WebSocket()`).
    let upgrade: Response;
    try {
      upgrade = await fetch(IRC_WSS.replace(/^wss:/, "https:"), {
        headers: { Upgrade: "websocket" },
      });
    } catch (e) {
      this.status.last_error = `ws fetch threw: ${String(e)}`;
      await this.persistStatus();
      await this.scheduleReconnectAlarm();
      return;
    }
    const ws = upgrade.webSocket;
    if (!ws) {
      this.status.last_error = `ws upgrade failed: ${upgrade.status}`;
      await this.persistStatus();
      await this.scheduleReconnectAlarm();
      return;
    }

    ws.accept();
    this.ws = ws;
    this.sawPrivmsg = false;
    this.status.started_at = Date.now();
    this.status.connected = true;
    this.status.last_error = null;
    this.status.reconnect_attempts = 0;

    // Cancel any pending reconnect alarm and start the watchdog.
    await this.state.storage.setAlarm(Date.now() + WATCHDOG_INTERVAL_MS);

    // IRC login. Twitch requires CAP REQ before NICK/PASS for tags + commands.
    ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands");
    ws.send(`PASS ${TWITCH_BOT_OAUTH_TOKEN}`);
    ws.send(`NICK ${TWITCH_BOT_NICK.toLowerCase()}`);
    ws.send(`JOIN #${TWITCH_BROADCASTER_LOGIN.toLowerCase()}`);

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
      await this.state.storage.delete("disconnectedAt");
    }

    // Keep-alive ping to prevent Twitch from timing out the connection.
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
      this.persistStatus().catch(() => {});
      for (const rawLine of data.split("\r\n")) {
        const line = rawLine.trim();
        if (!line) continue;
        this.handleLine(line).catch((e) => console.error("handleLine error", e));
      }
    });

    ws.addEventListener("close", (ev: CloseEvent) => {
      console.warn(`[irc] ws closed code=${ev.code} reason=${ev.reason ?? ""}`);
      this.status.connected = false;
      this.status.last_error = `close ${ev.code} ${ev.reason ?? ""}`;
      this.ws = null;
      if (this.disconnectedAt === null) {
        this.disconnectedAt = Date.now();
        this.state.storage.put("disconnectedAt", this.disconnectedAt).catch(() => {});
      }
      this.persistStatus().catch(() => {});
      this.scheduleReconnectAlarm().catch(() => {});
    });

    ws.addEventListener("error", (ev: Event) => {
      console.error("[irc] ws error", (ev as ErrorEvent).message ?? "");
      this.status.last_error = `ws error ${String((ev as ErrorEvent).message ?? "")}`;
      this.ws = null;
      if (this.disconnectedAt === null) {
        this.disconnectedAt = Date.now();
        this.state.storage.put("disconnectedAt", this.disconnectedAt).catch(() => {});
      }
      this.persistStatus().catch(() => {});
    });

    await this.persistStatus();
  }

  private teardown(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
    this.sawPrivmsg = false;
    this.status.connected = false;
  }

  private async scheduleReconnectAlarm(delayMs?: number): Promise<void> {
    // Don't stack alarms — if one is already pending, let it fire.
    const existing = await this.state.storage.getAlarm();
    if (existing !== null) return;

    this.status.reconnect_attempts += 1;
    const backoff =
      delayMs ??
      Math.min(RECONNECT_MAX_MS, 1000 * 2 ** Math.min(this.status.reconnect_attempts, 5));
    await this.state.storage.setAlarm(Date.now() + backoff);
    await this.persistStatus();
  }

  private async persistStatus(): Promise<void> {
    await this.state.storage.put("status", this.status);
  }

  private async handleLine(line: string): Promise<void> {
    // Keepalive
    if (line.startsWith("PING ")) {
      this.ws?.send(line.replace("PING", "PONG"));
      return;
    }

    const parsed = parseIrc(line);
    if (!parsed) return;

    if (parsed.command === "NOTICE") {
      console.warn(`[irc notice] ${parsed.trailing ?? ""}`);
      return;
    }
    if (parsed.command === "RECONNECT") {
      console.warn("[irc] server requested reconnect");
      return;
    }
    if (parsed.command !== "PRIVMSG") return;

    if (!this.sawPrivmsg) {
      const preview = (parsed.trailing ?? "").slice(0, 120);
      console.log(`[irc] first PRIVMSG from ${parsed.sourceLogin ?? "?"}: ${preview}`);
      this.sawPrivmsg = true;
    }

    const message = parsed.trailing ?? "";
    const match = /^!clip(?:\s+(.+))?$/i.exec(message.trim());
    if (!match) return;

    const login = parsed.sourceLogin ?? "";
    if (!login) return;

    const whitelisted = await this.isWhitelisted(login);
    const label = (match[1] ?? "").trim() || null;
    console.log(
      `[irc] !clip received from ${login} (whitelisted=${whitelisted}) label=${label ?? "(none)"}`,
    );
    const clipId = uuidv7();
    const triggeredAt = new Date().toISOString();

    // Lazily generate a date-based session ID (one per calendar day).
    const todaySession = `session-${new Date().toISOString().slice(0, 10)}`;
    if (this.currentSessionId !== todaySession) {
      this.currentSessionId = todaySession;
    }

    // Resolve content_tag for this session (defaults to 'gameplay' if no row exists).
    const sessionRow = await this.env.CLIP_DB
      .prepare(`SELECT content_tag FROM stream_sessions ORDER BY updated_at DESC LIMIT 1`)
      .first<{ content_tag: string }>();
    const contentTag = sessionRow?.content_tag ?? "gameplay";

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
          content_tag: contentTag,
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
          content_tag: contentTag,
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
        content_tag: contentTag,
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
