import type { ChatStreamEvent } from "./types";

/**
 * Parses the `data: {...}\n\n` SSE framing emitted by
 * src/smi_agent/conversation/sse.py (relayed byte-for-byte by the gateway).
 * Uses fetch + ReadableStream rather than EventSource because EventSource
 * cannot send the POST body / custom auth headers this endpoint requires.
 */
export async function consumeChatStream(
  response: Response,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  if (!response.body) throw new Error("Response has no body to stream");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      if (signal?.aborted) break;
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");

        const line = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        const jsonText = line.slice(5).trim();
        if (!jsonText) continue;
        try {
          onEvent(JSON.parse(jsonText) as ChatStreamEvent);
        } catch {
          // Malformed frame — skip rather than kill the whole stream.
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
