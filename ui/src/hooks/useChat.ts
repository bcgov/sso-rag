import { useCallback, useState } from "react";
import { flushSync } from "react-dom";
import type { ChatCompletionChunk, ChatMessage, Citation } from "../types";

const API_URL = "/query";
const DONE_SENTINEL = "[DONE]";

function parseChunk(line: string): ChatCompletionChunk | null {
  const stripped = line.replace(/^data:\s*/, "");
  if (stripped === DONE_SENTINEL || stripped === "") return null;
  try {
    return JSON.parse(stripped) as ChatCompletionChunk;
  } catch {
    return null;
  }
}

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sendMessage = useCallback(async (query: string) => {
    setError(null);

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: query,
    };

    const assistantId = crypto.randomUUID();
    const assistantPlaceholder: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      streaming: true,
    };

    setMessages((prev) => [...prev, userMessage, assistantPlaceholder]);
    setIsStreaming(true);

    try {
      const response = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
      });

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      if (!response.body) {
        throw new Error("No response body received.");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let done = false;

      while (!done) {
        const { done: streamDone, value } = await reader.read();
        if (streamDone) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        // Keep the last (potentially incomplete) line in the buffer
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) continue;

          // Server signals end of stream — cancel the reader and stop.
          if (trimmed === `data: ${DONE_SENTINEL}`) {
            done = true;
            break;
          }

          const chunk = parseChunk(trimmed);
          if (!chunk) continue;

          const choice = chunk.choices[0];
          if (!choice) continue;

          // A finish_reason of "stop" also signals the last content chunk.
          if (choice.finish_reason === "stop") {
            done = true;
            break;
          }

          const { content, citations } = choice.delta;

          if (content)
            console.debug("[stream] chunk:", JSON.stringify(content));

          flushSync(() => {
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantId) return m;
                return {
                  ...m,
                  content: m.content + (content ?? ""),
                  citations: citations
                    ? [...(m.citations ?? []), ...(citations as Citation[])]
                    : m.citations,
                };
              }),
            );
          });
        }
      }

      reader.cancel();
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "An unexpected error occurred.";
      setError(message);
      // Remove the empty assistant placeholder on error
      setMessages((prev) => prev.filter((m) => m.id !== assistantId));
    } finally {
      // Mark streaming done
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, streaming: false } : m,
        ),
      );
      setIsStreaming(false);
    }
  }, []);

  function clearMessages() {
    setMessages([]);
    setError(null);
  }

  return { messages, isStreaming, error, sendMessage, clearMessages };
}
