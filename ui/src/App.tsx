import { useEffect, useRef, useState } from "react";
import { ChatMessageBubble } from "./components/ChatMessageBubble";
import { ChatInput } from "./components/ChatInput";
import { DeleteHistoryDialog } from "./components/DeleteHistoryDialog";
import { useChat } from "./hooks/useChat";

const NOTICE_MS = 4000;

export default function App() {
  const {
    messages,
    isStreaming,
    error,
    sendMessage,
    clearMessages,
    deleteHistory,
  } = useChat();
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const mainRef = useRef<HTMLElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const messageCount = messages.length;

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), NOTICE_MS);
    return () => clearTimeout(timer);
  }, [notice]);

  async function handleDeleteHistory() {
    await deleteHistory();
    setConfirmingDelete(false);
    setNotice("Your chat history was deleted.");
  }

  // A new message (the user's prompt) scrolls into view smoothly.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messageCount]);

  // While an answer streams, follow it instantly — a smooth scroll restarted
  // on every frame stutters — and only if the user hasn't scrolled up to read.
  useEffect(() => {
    const el = mainRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [messages]);

  return (
    <div className="flex flex-col h-screen bg-white text-gray-900 antialiased">
      {/* Header */}
      <header className="flex items-center justify-between gap-3 border-b border-gray-200 px-4 py-3 flex-shrink-0">
        <div className="min-w-0">
          <h1 className="text-base font-semibold tracking-tight">SSO RAG</h1>
          <p className="truncate text-xs text-gray-500">AWS Bedrock Knowledge Base</p>
        </div>
        <div className="flex flex-shrink-0 items-center gap-1">
          {messages.length > 0 && (
            <button
              type="button"
              onClick={clearMessages}
              title="Start a new conversation in this tab"
              className="whitespace-nowrap rounded-lg px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 transition-colors"
            >
              New chat
            </button>
          )}
          {/* Always available: history from other tabs may be stored even
              when this tab is empty. */}
          <button
            type="button"
            onClick={() => setConfirmingDelete(true)}
            aria-haspopup="dialog"
            className="inline-flex flex-shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg px-2.5 py-1.5 text-xs font-medium text-gray-500 hover:bg-red-50 hover:text-red-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500 transition-colors"
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 20 20"
              fill="currentColor"
              className="h-3.5 w-3.5"
            >
              <path
                fillRule="evenodd"
                d="M8.75 1A2.75 2.75 0 0 0 6 3.75v.443c-.795.077-1.584.176-2.365.298a.75.75 0 1 0 .23 1.482l.149-.022.841 10.518A2.75 2.75 0 0 0 7.596 19h4.807a2.75 2.75 0 0 0 2.742-2.53l.841-10.52.149.023a.75.75 0 0 0 .23-1.482A41.03 41.03 0 0 0 14 4.193V3.75A2.75 2.75 0 0 0 11.25 1h-2.5ZM10 4c.84 0 1.673.025 2.5.075V3.75c0-.69-.56-1.25-1.25-1.25h-2.5c-.69 0-1.25.56-1.25 1.25v.325C8.327 4.025 9.16 4 10 4ZM8.58 7.72a.75.75 0 0 0-1.5.06l.3 7.5a.75.75 0 1 0 1.5-.06l-.3-7.5Zm4.34.06a.75.75 0 1 0-1.5-.06l-.3 7.5a.75.75 0 1 0 1.5.06l.3-7.5Z"
                clipRule="evenodd"
              />
            </svg>
            Delete history
          </button>
        </div>
      </header>

      {/* Polite live region: announced by screen readers, never steals focus. */}
      <div role="status" aria-live="polite" className="pointer-events-none fixed inset-x-0 top-16 z-10 flex justify-center px-4">
        {notice && (
          <p className="rounded-xl bg-gray-900 px-4 py-2 text-sm text-white shadow-lg">
            {notice}
          </p>
        )}
      </div>

      <DeleteHistoryDialog
        open={confirmingDelete}
        onClose={() => setConfirmingDelete(false)}
        onConfirm={handleDeleteHistory}
      />

      {/* Message list */}
      <main ref={mainRef} className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-center">
            <p className="text-2xl">💬</p>
            <p className="text-sm text-gray-500">
              Ask anything about your knowledge base.
            </p>
          </div>
        ) : (
          messages.map((msg) => (
            <ChatMessageBubble key={msg.id} message={msg} />
          ))
        )}

        {error && (
          <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {error}
          </div>
        )}

        <div ref={bottomRef} />
      </main>

      {/* Input */}
      <ChatInput onSend={sendMessage} disabled={isStreaming} />
    </div>
  );
}
