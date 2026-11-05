import { useState, useRef, type KeyboardEvent, type FormEvent } from "react";

interface Props {
  onSend: (query: string) => void;
  disabled: boolean;
  /** Longest message the API accepts, in characters; null while unknown. */
  maxChars?: number | null;
}

// Show the length counter once the message reaches this share of the limit.
const COUNTER_FROM = 0.8;

export function ChatInput({ onSend, disabled, maxChars = null }: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const length = value.trim().length;
  const tooLong = maxChars !== null && length > maxChars;
  const showCounter = maxChars !== null && length >= maxChars * COUNTER_FROM;

  function submit() {
    const trimmed = value.trim();
    if (!trimmed || disabled || tooLong) return;
    onSend(trimmed);
    setValue("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    submit();
  }

  function handleInput() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="border-t border-gray-200 bg-white px-4 py-3"
    >
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          disabled={disabled}
          placeholder="Ask a question…"
          aria-invalid={tooLong}
          aria-describedby={showCounter ? "chat-input-counter" : undefined}
          className="flex-1 resize-none rounded-xl border border-gray-300 bg-gray-50 px-3 py-2 text-sm text-gray-900 placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:opacity-50 max-h-40 overflow-y-auto"
        />
        <button
          type="submit"
          disabled={disabled || !value.trim() || tooLong}
          className="flex-shrink-0 rounded-xl bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          Send
        </button>
      </div>
      {showCounter && (
        <p
          id="chat-input-counter"
          aria-live="polite"
          className={`mt-1 text-right text-xs ${tooLong ? "text-red-600" : "text-gray-500"}`}
        >
          {tooLong
            ? `Message is too long: ${length.toLocaleString()} / ${maxChars.toLocaleString()} characters. Please shorten it.`
            : `${length.toLocaleString()} / ${maxChars.toLocaleString()} characters`}
        </p>
      )}
    </form>
  );
}
