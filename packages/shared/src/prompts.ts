// Prompt loader — always fetches the latest version by key.
// Workers cache prompts in memory for the life of the isolate.

const cache = new Map<string, { body: string; version: number; at: number }>();
const TTL_MS = 60_000;

export async function getPrompt(
  db: D1Database,
  key: string,
): Promise<{ body: string; version: number }> {
  const cached = cache.get(key);
  if (cached && Date.now() - cached.at < TTL_MS) {
    return { body: cached.body, version: cached.version };
  }
  const row = await db
    .prepare(
      `SELECT body, version FROM prompts WHERE key = ?1 ORDER BY version DESC LIMIT 1`,
    )
    .bind(key)
    .first<{ body: string; version: number }>();
  if (!row) throw new Error(`Prompt not found: ${key}`);
  cache.set(key, { body: row.body, version: row.version, at: Date.now() });
  return row;
}
