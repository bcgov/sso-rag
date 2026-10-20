import { useEffect, useId, useRef, useState } from "react";

interface Props {
  open: boolean;
  onClose: () => void;
  onConfirm: () => Promise<void>;
}

// Built on the native <dialog>: showModal() traps focus, makes the page
// behind it inert, closes on Escape and restores focus to the opener.
export function DeleteHistoryDialog({ open, onClose, onConfirm }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleId = useId();
  const descId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      setError(null);
      dialog.showModal();
      // Default to the safe choice for a destructive action.
      cancelRef.current?.focus();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  async function confirm() {
    setDeleting(true);
    setError(null);
    try {
      await onConfirm();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not delete your history.",
      );
    } finally {
      setDeleting(false);
    }
  }

  return (
    <dialog
      ref={dialogRef}
      role="alertdialog"
      aria-labelledby={titleId}
      aria-describedby={descId}
      // Escape: keep the dialog open while a delete is in progress.
      onCancel={(e) => {
        e.preventDefault();
        if (!deleting) onClose();
      }}
      // Backdrop click (the event target is the <dialog> itself).
      onClick={(e) => {
        if (e.target === e.currentTarget && !deleting) onClose();
      }}
      className="w-[calc(100%-2rem)] max-w-sm rounded-2xl p-0 shadow-xl backdrop:bg-gray-900/40"
    >
      <div className="p-5">
        <h2 id={titleId} className="text-base font-semibold text-gray-900">
          Delete chat history?
        </h2>
        <div id={descId} className="mt-2 space-y-2 text-sm text-gray-600">
          <p>
            This permanently deletes your conversations with the assistant,
            including those in other tabs of this browser.
          </p>
          <p>This can’t be undone.</p>
        </div>

        {error && (
          <p
            role="alert"
            className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          >
            {error}
          </p>
        )}

        <div className="mt-5 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            ref={cancelRef}
            type="button"
            onClick={onClose}
            disabled={deleting}
            className="rounded-xl border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={confirm}
            disabled={deleting}
            aria-busy={deleting}
            className="rounded-xl bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500 focus-visible:ring-offset-2 disabled:cursor-wait disabled:opacity-60"
          >
            {deleting ? "Deleting…" : error ? "Try again" : "Delete history"}
          </button>
        </div>
      </div>
    </dialog>
  );
}
