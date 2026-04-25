// Proxy all /api/* requests to the approval-worker. We use a Pages Function
// rather than a _redirects rewrite because cross-origin 200-status proxies in
// _redirects were silently falling through to the static index.html, which
// broke the dashboard with "Unexpected token '<'" JSON-parse errors.

interface Env {
  APPROVAL_WORKER_ORIGIN?: string;
}

const DEFAULT_ORIGIN = "https://clip-approval.missioncontrol5mof.workers.dev";

export const onRequest: PagesFunction<Env> = async ({ request, env }) => {
  const incoming = new URL(request.url);
  const origin = (env.APPROVAL_WORKER_ORIGIN ?? DEFAULT_ORIGIN).replace(/\/$/, "");
  const target = new URL(origin + incoming.pathname + incoming.search);

  const proxied = new Request(target.toString(), request);
  proxied.headers.set("host", target.host);
  return fetch(proxied);
};
