// Post worker: POST_DISPATCH queue consumer → n8n webhook.
// On 5xx from n8n: retry via Queues (up to 3). After exhaustion,
// we set status='ready_to_post' so the dashboard can trigger a manual retry.

import {
  clipsDb,
  metricoolDb,
  r2Signer,
  type CaptionsTriple,
  type MetricoolBrand,
  type N8nPostPayload,
  type PostDispatchJob,
} from "@clipfactory/shared";
import type { Env } from "./env.js";

export default {
  async queue(batch: MessageBatch<PostDispatchJob>, env: Env): Promise<void> {
    for (const msg of batch.messages) {
      try {
        await handleOne(msg.body, env);
        msg.ack();
      } catch (err) {
        console.error("post handleOne error", msg.body.clip_id, err);
        await clipsDb.appendErrorLog(env.CLIP_DB, msg.body.clip_id, "post", String(err));
        if (msg.attempts >= 3) {
          await clipsDb.setStatus(env.CLIP_DB, msg.body.clip_id, "ready_to_post");
          await clipsDb.appendApprovalLog(
            env.CLIP_DB,
            msg.body.clip_id,
            "failed",
            "post-worker",
            { error: String(err) },
          );
          // Alert ops — n8n 3-strikes failure
          await sendOpsAlert(env, {
            clip_id: msg.body.clip_id,
            brand_name: null,
            blog_id: null,
            stage: "dispatch",
            error: String(err),
            severity: "high",
          });
          msg.ack();
        } else {
          msg.retry({ delaySeconds: 15 * msg.attempts });
        }
      }
    }
  },
};

async function handleOne(job: PostDispatchJob, env: Env): Promise<void> {
  const clip = await clipsDb.getClip(env.CLIP_DB, job.clip_id);
  if (!clip) throw new Error(`clip ${job.clip_id} not found`);
  if (!clip.final_clip_r2_key) throw new Error(`clip ${job.clip_id} has no final_clip_r2_key`);

  // Pick next brand BEFORE the n8n POST (cursor advances regardless of outcome per plan §4).
  const brand = await metricoolDb.pickNextBrand(env.CLIP_DB);
  if (!brand) {
    // Table empty or D1 error — alert ops and abort (will retry via queue).
    await sendOpsAlert(env, {
      clip_id: job.clip_id,
      brand_name: null,
      blog_id: null,
      stage: "pre-dispatch",
      error: "pickNextBrand returned nothing — metricool_brands table may be empty",
      severity: "high",
    });
    throw new Error("pickNextBrand returned nothing");
  }

  const videoUrl = env.R2_PUBLIC_BASE
    ? `${env.R2_PUBLIC_BASE.replace(/\/$/, "")}/${clip.final_clip_r2_key}`
    : await r2Signer.signR2GetUrl(
        {
          accountId: env.R2_ACCOUNT_ID,
          accessKeyId: env.R2_ACCESS_KEY_ID,
          secretAccessKey: env.R2_SECRET_ACCESS_KEY,
          bucket: env.R2_BUCKET_NAME,
        },
        clip.final_clip_r2_key,
        3600,
      );

  const captions: CaptionsTriple = {
    instagram: clip.instagram_post_text ?? "",
    youtube: clip.youtube_post_text ?? "",
    tiktok: clip.tiktok_post_text ?? "",
  };

  const payload: N8nPostPayload = {
    clip_id: job.clip_id,
    video_url: videoUrl,
    brand: { id: brand.id, brand_name: brand.brand_name, blog_id: brand.blog_id },
    titles_per_platform: {
      youtube: clip.youtube_post_text ?? "",
      tiktok: clip.tiktok_post_text ?? "",
      instagram: clip.instagram_post_text ?? "",
    },
    publish_now: true,
    // backwards-compat for one release
    captions,
    sponsor_session_id: clip.stream_session_id,
    approved_by: job.approved_by,
  };

  const res = await fetch(env.N8N_WEBHOOK_URL, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-n8n-secret": env.N8N_WEBHOOK_SECRET,
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`n8n ${res.status}: ${text.slice(0, 300)}`);
  }

  // Success: record brand dispatch and set status=dispatched.
  const now = new Date().toISOString();
  await metricoolDb.markBrandDispatched(env.CLIP_DB, brand.id, now);
  await clipsDb.setDispatchedBrand(env.CLIP_DB, job.clip_id, brand);
  await clipsDb.setStatus(env.CLIP_DB, job.clip_id, "dispatched");
  await clipsDb.appendApprovalLog(
    env.CLIP_DB,
    job.clip_id,
    "dispatched_to_metricool",
    "post-worker",
    { brand: brand.brand_name, blog_id: brand.blog_id, n8n_status: res.status },
  );
}

/** Call the OPS service binding's /internal/ops-alert route. Fire-and-forget on failure. */
async function sendOpsAlert(
  env: Env,
  args: {
    clip_id: string;
    brand_name: string | null;
    blog_id: number | null;
    stage: string;
    error: string;
    severity: "high" | "medium";
  },
): Promise<void> {
  try {
    const dashboardUrl = env.DASHBOARD_URL
      ? `${env.DASHBOARD_URL.replace(/\/$/, "")}/review?id=${args.clip_id}`
      : null;
    await env.OPS.fetch("https://clip-approval/internal/ops-alert", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${env.GPU_INTERNAL_SECRET}`,
      },
      body: JSON.stringify({ ...args, dashboard_url: dashboardUrl }),
    });
  } catch (err) {
    console.error("sendOpsAlert to OPS service failed", err);
  }
}
