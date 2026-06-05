import { useEffect, useRef } from "react";
import { ChatMessageBubble } from "./components/ChatMessageBubble";
import { ChatInput } from "./components/ChatInput";
import { useChat } from "./hooks/useChat";

export default function App() {
  const { messages, isStreaming, error, sendMessage, clearMessages } =
    useChat();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="flex flex-col h-screen bg-white text-gray-900 antialiased">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-gray-200 px-4 py-3 flex-shrink-0">
        <div>
          <h1 className="text-base font-semibold tracking-tight">SSO RAG</h1>
          <p className="text-xs text-gray-500">AWS Bedrock Knowledge Base</p>
        </div>
        {messages.length > 0 && (
          <button
            onClick={clearMessages}
            className="text-xs text-gray-400 hover:text-gray-700 transition-colors"
          >
            Clear
          </button>
        )}
      </header>

      {/* Message list */}
      <main className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
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
