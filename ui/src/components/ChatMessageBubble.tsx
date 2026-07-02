import type { ChatMessage, Citation } from "../types";

interface Props {
  message: ChatMessage;
}

export function ChatMessageBubble({ message }: Props) {
  const isUser = message.role === "user";

  return (
    <div className={`flex w-full ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[75%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
          isUser
            ? "bg-blue-600 text-white rounded-br-sm"
            : "bg-gray-100 text-gray-900 rounded-bl-sm"
        }`}
      >
        <p className="whitespace-pre-wrap break-words">{message.content}</p>

        {message.streaming && (
          <span className="inline-block w-2 h-4 ml-1 bg-current opacity-70 animate-pulse rounded-sm align-middle" />
        )}

        {!message.streaming &&
          message.citations &&
          message.citations.length > 0 && (
            <CitationList citations={message.citations} />
          )}
      </div>
    </div>
  );
}

function CitationList({ citations }: { citations: Citation[] }) {
  return (
    <div className="mt-3 border-t border-gray-300 pt-2 space-y-1">
      <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
        Sources
      </p>
      {citations.map((c, i) => (
        <div key={i} className="text-xs text-gray-600">
          {c.location?.uri ? (
            <a
              href={c.location.uri}
              target="_blank"
              rel="noopener noreferrer"
              className="underline hover:text-blue-700 break-all"
            >
              {c.location.uri}
            </a>
          ) : (
            <span className="italic">Source {i + 1}</span>
          )}
          {c.location?.pageNumber !== undefined && (
            <span className="ml-1 text-gray-400">
              (p.{c.location.pageNumber})
            </span>
          )}
        </div>
      ))}
    </div>
  );
}
