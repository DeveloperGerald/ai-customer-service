/**
 * T10 SSE 流式解析客户端（前端侧通用 helper）。
 *
 * 实现要点（面试讲解点）：
 *  1. 用 `fetch` + `ReadableStream`，不依赖浏览器 `EventSource`（EventSource 只能 GET，无法带 body + JWT）
 *  2. 处理任意 TCP 分块：维护 `_buffer` 字符串，按 `\n\n` 切帧；一帧分两次 read 也不会拆坏
 *  3. 处理 UTF-8 多字节边界：`TextDecoder.decode` 使用 stream=true，结尾再 flush
 *  4. 事件名顺序：start → escalated(可选) → tool(可选) → reply → debug → done + [DONE] 终帧
 */
export type SseEventHandler = (event: SseEvent) => void | Promise<void>;

export interface SseEvent {
  event: string;
  data: any;
}

export interface StreamChatOptions {
  url: string;
  tenantId: string;
  bearer: string;
  /** SSE POST body；/stream 用 { text, idempotency_key }，/actions/confirm 用 { decision, reason? } */
  body: Record<string, any>;
  signal?: AbortSignal;
  onEvent: SseEventHandler;
}

export async function streamChat(options: StreamChatOptions): Promise<void> {
  const { url, tenantId, bearer, body, signal, onEvent } = options;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Tenant-Id": tenantId,
      Authorization: bearer,
    },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok || !resp.body) {
    let text = "";
    try {
      text = await resp.text();
    } catch {
      /* ignore */
    }
    throw new Error(`HTTP ${resp.status}: ${text || resp.statusText}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      const chunk = value ? decoder.decode(value, { stream: true }) : "";
      buffer += chunk;

      let sepIndex: number;
      while ((sepIndex = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, sepIndex);
        buffer = buffer.slice(sepIndex + 2);
        const parsed = parseFrame(frame);
        if (!parsed) continue;
        const finalData = parsed.dataStr === "[DONE]" ? "[DONE]" : safeJson(parsed.dataStr);
        await onEvent({ event: parsed.event, data: finalData });
      }

      if (done) {
        const tail = decoder.decode();
        if (tail) buffer += tail;
        if (buffer.trim().length > 0) {
          const parsed = parseFrame(buffer);
          if (parsed) {
            const finalData = parsed.dataStr === "[DONE]" ? "[DONE]" : safeJson(parsed.dataStr);
            await onEvent({ event: parsed.event, data: finalData });
          }
        }
        break;
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* ignore */
    }
  }
}

function parseFrame(frame: string): { event: string; dataStr: string } | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const rawLine of frame.split(/\r?\n/)) {
    if (!rawLine) continue;
    if (rawLine.startsWith(":")) continue; // SSE comment / heartbeat
    const colon = rawLine.indexOf(":");
    if (colon === -1) {
      event = rawLine.trim();
      continue;
    }
    const key = rawLine.slice(0, colon).trim();
    const value = rawLine.slice(colon + 1).replace(/^ /, "");
    if (key === "event") event = value;
    else if (key === "data") dataLines.push(value);
  }
  if (dataLines.length === 0 && event === "message") return null;
  return { event, dataStr: dataLines.join("\n") };
}

function safeJson(s: string): any {
  if (s === "") return {};
  try {
    return JSON.parse(s);
  } catch {
    return s;
  }
}
