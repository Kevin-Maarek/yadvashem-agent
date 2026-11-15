import type { ChatResult } from "@/lib/api/types";

export async function chatEvents(data: unknown, onText: (delta: string) => void): Promise<ChatResult> {
  const response = await fetch("/api/chat/stream", {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(data),
  });
  if (!response.ok || !response.body) throw new Error("chat_error");
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let result: ChatResult | null = null;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += value;
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        const name = block.split("\n").find((line) => line.startsWith("event: "))?.slice(7);
        const dataLine = block.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
        if (!dataLine) continue;
        const payload = JSON.parse(dataLine);
        if (name === "text") onText(payload.delta);
        if (name === "failure") throw new Error(payload.code);
        if (name === "result") result = payload as ChatResult;
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  if (!result) throw new Error("chat_error");
  return result;
}
