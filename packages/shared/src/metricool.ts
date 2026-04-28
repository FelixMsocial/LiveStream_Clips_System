// Round-robin brand picker for Metricool dispatch.
// This is the ONLY writer to metricool_rr_state.

type D1 = D1Database;

export interface MetricoolBrand {
  id: number;
  brand_name: string;
  blog_id: number;
}

export async function pickNextBrand(db: D1): Promise<MetricoolBrand | null> {
  // Advance the cursor atomically. Wraps to MIN(id) when past the last brand.
  const stateRow = await db
    .prepare(
      `UPDATE metricool_rr_state
       SET last_brand_id = COALESCE(
         (SELECT MIN(id) FROM metricool_brands WHERE id > metricool_rr_state.last_brand_id),
         (SELECT MIN(id) FROM metricool_brands)
       )
       WHERE id = 1
       RETURNING last_brand_id`,
    )
    .first<{ last_brand_id: number }>();

  if (!stateRow) return null;

  return await db
    .prepare(`SELECT id, brand_name, blog_id FROM metricool_brands WHERE id = ?1`)
    .bind(stateRow.last_brand_id)
    .first<MetricoolBrand>();
}

export async function markBrandDispatched(
  db: D1,
  brand_id: number,
  when: string,
): Promise<void> {
  await db
    .prepare(`UPDATE metricool_brands SET last_scheduled_at = ?1 WHERE id = ?2`)
    .bind(when, brand_id)
    .run();
}
