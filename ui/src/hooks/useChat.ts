import { useCallback, useEffect, useRef, useState } from "react";
import { createTypewriter, type Typewriter } from "../lib/typewriter";
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
// Relative (no leading slash) so it resolves under the API Gateway stage path
// in AWS (/<stage>/chat) and to /chat locally.
const API_URL = "chat";
const HISTORY_URL = "chat/history";
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
  const typewriterRef = useRef<Typewriter | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Stops the in-flight answer, if any (the server cancels the agent run
  // when the client disconnects).
  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    typewriterRef.current?.cancel();
  }, []);

  useEffect(() => stopStreaming, [stopStreaming]);

  const sendMessage = useCallback(async (prompt: string) => {
    setError(null);
    const controller = new AbortController();
    abortRef.current = controller;

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

    const updateAssistant = (patch: Partial<ChatMessage>) =>
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantId ? { ...m, ...patch } : m)),
      );

    // Received text is revealed smoothly, one animation frame at a time.
    const typewriter = createTypewriter((visible) =>
      updateAssistant({ content: visible }),
    );
    typewriterRef.current = typewriter;
    const citations: Citation[] = [];

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
        signal: controller.signal,
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

          const { content, citations: newCitations } = choice.delta;
          if (content) typewriter.push(content);
          if (newCitations) citations.push(...newCitations);
        }
      }

      reader.cancel();

      // Let the remaining text finish revealing before showing the sources.
      await typewriter.finish();
      updateAssistant({
        streaming: false,
        citations: citations.length ? citations : undefined,
      });
    } catch (err) {
      typewriter.cancel();
      // Stopped on purpose (new chat / delete history): the messages are
      // already gone, so there is nothing to report.
      if (controller.signal.aborted) return;
      const message =
        err instanceof Error ? err.message : "An unexpected error occurred.";
      setError(message);
      // Remove the assistant placeholder (and any partial answer) on error
      setMessages((prev) => prev.filter((m) => m.id !== assistantId));
    } finally {
      if (typewriterRef.current === typewriter) typewriterRef.current = null;
      if (abortRef.current === controller) {
        abortRef.current = null;
        setIsStreaming(false);
      }
    }
  }, []);

  const clearMessages = useCallback(async () => {
    stopStreaming();
    setIsStreaming(false);
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
  }, [stopStreaming]);

  // Permanently deletes this browser's stored history (every tab's
  // conversation) and its session cookie. Throws if the server couldn't
  // delete everything, leaving the current conversation on screen so the
  // user can retry.
  const deleteHistory = useCallback(async () => {
    const response = await fetch(HISTORY_URL, {
      method: "DELETE",
      credentials: "same-origin",
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}: ${response.statusText}`;
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        // Keep the status text.
      }
      throw new Error(detail);
    }

    stopStreaming();
    setIsStreaming(false);
    setMessages([]);
    setError(null);
    rotateTabId();
  }, [stopStreaming]);

  return {
    messages,
    isStreaming,
    error,
    sendMessage,
    clearMessages,
    deleteHistory,
  };
}
