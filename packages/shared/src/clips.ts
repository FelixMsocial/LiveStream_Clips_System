// D1 helpers. All Workers import these so SQL stays in one place.

import type { ClipRow, ClipStatus } from "./types.js";

type D1 = D1Database;

export async function insertClip(
  db: D1,
  row: {
    id: string;
    triggered_by: string;
    triggered_at: string;
    label: string | null;
    status: ClipStatus;
    stream_session_id?: string | null;
  },
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO clips
         (id, triggered_by, triggered_at, label, status, stream_session_id)
       VALUES (?1, ?2, ?3, ?4, ?5, ?6)`,
    )
    .bind(
      row.id,
      row.triggered_by,
      row.triggered_at,
      row.label ?? null,
      row.status,
      row.stream_session_id ?? null,
    )
    .run();
}

export async function getClip(db: D1, clipId: string): Promise<ClipRow | null> {
  return await db
    .prepare(`SELECT * FROM clips WHERE id = ?1`)
    .bind(clipId)
    .first<ClipRow>();
}

export async function setStatus(
  db: D1,
  clipId: string,
  status: ClipStatus,
  extra?: Record<string, string | number | null>,
): Promise<void> {
  const fields = ["status = ?1", "updated_at = datetime('now')"];
  const bindings: Array<string | number | null> = [status];
  let idx = 2;
  if (extra) {
    for (const [k, v] of Object.entries(extra)) {
      fields.push(`${k} = ?${idx}`);
      bindings.push(v);
      idx += 1;
    }
  }
  bindings.push(clipId);
  await db
    .prepare(`UPDATE clips SET ${fields.join(", ")} WHERE id = ?${idx}`)
    .bind(...bindings)
    .run();
}

export async function updateClip(
  db: D1,
  clipId: string,
  fields: Record<string, string | number | null>,
): Promise<void> {
  const entries = Object.entries(fields);
  if (entries.length === 0) return;
  const setExpr = entries.map(([k], i) => `${k} = ?${i + 1}`);
  setExpr.push(`updated_at = datetime('now')`);
  const bindings: Array<string | number | null> = entries.map(([, v]) => v);
  bindings.push(clipId);
  await db
    .prepare(`UPDATE clips SET ${setExpr.join(", ")} WHERE id = ?${bindings.length}`)
    .bind(...bindings)
    .run();
}

export async function appendApprovalLog(
  db: D1,
  clipId: string,
  eventType: string,
  actor: string | null,
  details: Record<string, unknown> | null,
): Promise<void> {
  await db
    .prepare(
      `INSERT INTO approval_log (clip_id, event_type, actor, details)
       VALUES (?1, ?2, ?3, ?4)`,
    )
    .bind(clipId, eventType, actor, details ? JSON.stringify(details) : null)
    .run();
}

export async function appendErrorLog(
  db: D1,
  clipId: string,
  stage: string,
  message: string,
): Promise<void> {
  // Read-modify-write of error_log JSON. D1 is SQLite, not Postgres — no JSONB ops.
  const row = await db
    .prepare(`SELECT error_log FROM clips WHERE id = ?1`)
    .bind(clipId)
    .first<{ error_log: string | null }>();
  const existing = row?.error_log ? safeParseArray(row.error_log) : [];
  existing.push({ stage, message, at: new Date().toISOString() });
  await db
    .prepare(
      `UPDATE clips SET error_log = ?1, updated_at = datetime('now') WHERE id = ?2`,
    )
    .bind(JSON.stringify(existing), clipId)
    .run();
}

function safeParseArray(raw: string): unknown[] {
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export async function isWhitelisted(
  db: D1,
  username: string,
): Promise<boolean> {
  const row = await db
    .prepare(
      `SELECT 1 AS ok FROM mod_whitelist WHERE twitch_username = ?1 AND active = 1 LIMIT 1`,
    )
    .bind(username.toLowerCase())
    .first<{ ok: number }>();
  return !!row;
}

export async function isApprover(
  db: D1,
  telegramUserId: string,
): Promise<boolean> {
  const row = await db
    .prepare(
      `SELECT 1 AS ok FROM approvers WHERE telegram_user_id = ?1 AND active = 1 LIMIT 1`,
    )
    .bind(telegramUserId)
    .first<{ ok: number }>();
  return !!row;
}

export async function getActiveSponsor(
  db: D1,
  sessionId: string,
): Promise<{
  stream_session_id: string;
  sponsor_animation_r2_key: string;
  position: string;
  opacity: number;
  scale_pct: number;
} | null> {
  return await db
    .prepare(
      `SELECT * FROM sponsor_config
       WHERE stream_session_id = ?1
         AND datetime('now') BETWEEN active_from AND COALESCE(active_to, datetime('now','+1 day'))`,
    )
    .bind(sessionId)
    .first();
}
