// Entry point — hosts the ChatListenerDO and a heartbeat cron.
// The Worker itself is stateless; the DO holds the IRC WebSocket.

import type { Env } from "./env.js";
export { ChatListenerDO } from "./do.js";

const LISTENER_DO_NAME = "primary";

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/healthz") {
      const stub = env.CHAT_LISTENER.get(env.CHAT_LISTENER.idFromName(LISTENER_DO_NAME));
      const doStatus = await stub.fetch("https://do/status").then((r) => r.json()).catch((e) => ({
        error: String(e),
      }));
      return Response.json({ ok: true, do: doStatus });
    }
    if (url.pathname === "/start" && req.method === "POST") {
      // Manual kick — dev-only.
      const stub = env.CHAT_LISTENER.get(env.CHAT_LISTENER.idFromName(LISTENER_DO_NAME));
      await stub.fetch("https://do/start");
      return new Response("ok");
    }
    return new Response("clip-listener", { status: 200 });
  },

  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    // Minute-by-minute heartbeat. Ensures the DO socket is alive; restarts if not.
    ctx.waitUntil(
      (async () => {
        const stub = env.CHAT_LISTENER.get(env.CHAT_LISTENER.idFromName(LISTENER_DO_NAME));
        try {
          await stub.fetch("https://do/heartbeat");
        } catch (err) {
          console.error("heartbeat failed", err);
        }
      })(),
    );
  },
};
