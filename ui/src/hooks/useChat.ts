import { useCallback, useState } from "react";
import { flushSync } from "react-dom";
import type {
  ChatCompletionChunk,
  ChatMessage,
  ChatRequest,
  Citation,
  StreamError,
} from "../types";

// The API keeps conversation history server-side, keyed by an HttpOnly
// session cookie it sets on the /chat response plus this tab's id. The UI is
// served from the same origin (nginx / Vite proxy), so the browser sends the
// cookie back automatically; `credentials: "same-origin"` makes that explicit.
const API_URL = "/chat";
const DONE_SENTINEL = "[DONE]";
const TAB_ID_HEADER = "X-Tab-Id";
const TAB_ID_KEY = "sso-rag-tab-id";

// sessionStorage is per tab and survives reloads, so each tab keeps its own
// conversation. (Duplicating a tab copies sessionStorage, so a duplicated tab
// continues the original's conversation until it is cleared.)
function getTabId(): string {
  let tabId = sessionStorage.getItem(TAB_ID_KEY);
  if (!tabId) {
    tabId = crypto.randomUUID();
    sessionStorage.setItem(TAB_ID_KEY, tabId);
  }
  return tabId;
}

function rotateTabId(): void {
  sessionStorage.setItem(TAB_ID_KEY, crypto.randomUUID());
}

function parseChunk(line: string): ChatCompletionChunk | StreamError | null {
  const stripped = line.replace(/^data:\s*/, "");
  if (stripped === DONE_SENTINEL || stripped === "") return null;
  try {
    return JSON.parse(stripped) as ChatCompletionChunk | StreamError;
  } catch {
    return null;
  }
}

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sendMessage = useCallback(async (prompt: string) => {
    setError(null);

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: prompt,
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
      const body: ChatRequest = { prompt };
      const response = await fetch(API_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          [TAB_ID_HEADER]: getTabId(),
        },
        credentials: "same-origin",
        body: JSON.stringify(body),
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

          // The status is already 200 once streaming starts, so the server
          // reports failures in-band.
          if ("error" in chunk) {
            throw new Error(chunk.error);
          }

          const choice = chunk.choices[0];
          if (!choice) continue;

          // A finish_reason of "stop" also signals the last content chunk.
          if (choice.finish_reason === "stop") {
            done = true;
            break;
          }

          const { content, citations } = choice.delta;
          if (!content && !citations) continue;

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
      // Remove the assistant placeholder (and any partial answer) on error
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

  const clearMessages = useCallback(async () => {
    setMessages([]);
    setError(null);

    // Delete this tab's stored history, then switch to a new tab id so the
    // next message starts a fresh conversation even if the delete failed.
    try {
      await fetch(API_URL, {
        method: "DELETE",
        headers: { [TAB_ID_HEADER]: getTabId() },
        credentials: "same-origin",
      });
    } catch {
      // Not fatal: the new tab id below already starts a new conversation.
    } finally {
      rotateTabId();
    }
  }, []);

  return { messages, isStreaming, error, sendMessage, clearMessages };
}
