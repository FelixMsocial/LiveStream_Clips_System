// Thin Telegram Bot API client. All methods throw on !ok with full body.

const BASE = "https://api.telegram.org";

export interface InlineKeyboardButton {
  text: string;
  url?: string;
  callback_data?: string;
}

export type InlineKeyboard = InlineKeyboardButton[][];

export class TelegramClient {
  constructor(private readonly token: string) {}

  private async call<T>(method: string, body: unknown): Promise<T> {
    const res = await fetch(`${BASE}/bot${this.token}/${method}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const text = await res.text();
    if (!res.ok) {
      throw new Error(`telegram ${method} ${res.status}: ${text}`);
    }
    const parsed = JSON.parse(text) as { ok: boolean; result: T; description?: string };
    if (!parsed.ok) {
      throw new Error(`telegram ${method} not ok: ${parsed.description}`);
    }
    return parsed.result;
  }

  sendMessage(params: {
    chat_id: string | number;
    text: string;
    parse_mode?: "Markdown" | "HTML";
    reply_markup?: { inline_keyboard: InlineKeyboard };
    disable_web_page_preview?: boolean;
  }): Promise<{ message_id: number }> {
    return this.call("sendMessage", params);
  }

  sendVideo(params: {
    chat_id: string | number;
    video: string; // URL or file_id
    caption?: string;
    parse_mode?: "Markdown" | "HTML";
    reply_markup?: { inline_keyboard: InlineKeyboard };
    supports_streaming?: boolean;
  }): Promise<{ message_id: number }> {
    return this.call("sendVideo", params);
  }

  answerCallbackQuery(params: {
    callback_query_id: string;
    text?: string;
    show_alert?: boolean;
  }): Promise<boolean> {
    return this.call("answerCallbackQuery", params);
  }

  editMessageReplyMarkup(params: {
    chat_id: string | number;
    message_id: number;
    reply_markup?: { inline_keyboard: InlineKeyboard };
  }): Promise<unknown> {
    return this.call("editMessageReplyMarkup", params);
  }

  editMessageText(params: {
    chat_id: string | number;
    message_id: number;
    text: string;
    parse_mode?: "Markdown" | "HTML";
    reply_markup?: { inline_keyboard: InlineKeyboard };
  }): Promise<unknown> {
    return this.call("editMessageText", params);
  }
}
