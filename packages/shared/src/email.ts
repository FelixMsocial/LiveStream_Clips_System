// Resend email client for fallback alerting when Telegram fails.

export interface SendEmailResult {
  ok: boolean;
  error?: string;
}

/**
 * Send an email via the Resend HTTP API.
 * @see https://resend.com/docs/api-reference/emails/send-email
 */
export async function sendEmail(
  apiKey: string,
  to: string,
  subject: string,
  htmlBody: string,
): Promise<SendEmailResult> {
  try {
    const res = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        from: "ClipFactory <noreply@clipfactory.dev>",
        to: [to],
        subject,
        html: htmlBody,
      }),
    });
    if (!res.ok) {
      const text = await res.text();
      return { ok: false, error: `resend ${res.status}: ${text}` };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: `resend fetch error: ${String(err)}` };
  }
}
