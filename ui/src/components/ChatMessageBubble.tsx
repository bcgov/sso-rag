import { memo } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { stabilizeStreamingMarkdown } from "../lib/markdown";
import type { ChatMessage, Citation } from "../types";

interface Props {
  message: ChatMessage;
}

// Answers link to the docs site; open those outside the chat.
const markdownComponents: Components = {
  a: ({ node: _node, ...props }) => (
    <a {...props} target="_blank" rel="noopener noreferrer" />
  ),
  table: ({ node: _node, ...props }) => (
    <div className="overflow-x-auto">
      <table {...props} />
    </div>
  ),
};

// Memoized: while one answer streams, the other bubbles keep the same message
// object and skip re-parsing their Markdown on every frame.
export const ChatMessageBubble = memo(function ChatMessageBubble({
  message,
}: Props) {
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
        {isUser ? (
          <p className="whitespace-pre-wrap break-words">{message.content}</p>
        ) : (
          // react-markdown doesn't render raw HTML, so model output can't
          // inject markup.
          <div className="prose prose-sm max-w-none break-words prose-p:my-2 prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5 prose-pre:my-2 prose-headings:mt-3 prose-headings:mb-2 prose-a:text-blue-700 first:prose-p:mt-0 last:prose-p:mb-0">
            <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {message.streaming
                ? stabilizeStreamingMarkdown(message.content)
                : message.content}
            </Markdown>
          </div>
        )}

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
});

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
              {c.title || c.location.uri}
            </a>
          ) : (
            <span className="italic">{c.title || `Source ${i + 1}`}</span>
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
