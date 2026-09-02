import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Loader2, MessageSquare, MoreHorizontal, Phone } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { useSoftphone } from "@/softphone/SoftphoneProvider";
import { patchThread, type Conversation } from "@/api/conversations";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

export function ConversationHeader({
  conversation,
  canSend = true,
  onBack,
  className,
}: {
  conversation: Conversation | null;
  /** F2: viewers (my_role "viewer") can see the conversation but not act on it - the
   * Call button is disabled for them, same gate as the Composer (F1). */
  canSend?: boolean;
  /** Below md, ConversationsPage hides the conversation list once a conversation is
   * selected - this is the way back to it. Omit to hide the back button entirely (e.g.
   * when a caller renders the header standalone, with no list to return to). */
  onBack?: () => void;
  className?: string;
}) {
  const { api } = useAuth();
  const softphone = useSoftphone();
  const queryClient = useQueryClient();
  const [moreOpen, setMoreOpen] = React.useState(false);
  const menuRef = React.useRef<HTMLDivElement>(null);

  const startCall = React.useCallback(async () => {
    if (!conversation || !canSend) return;
    try {
      await softphone.dial(conversation.contact_e164, conversation.our_e164);
    } catch {
      /* softphone surface already handles the visible error */
    }
  }, [conversation, canSend, softphone]);

  const focusComposer = React.useCallback(() => {
    document
      .querySelector<HTMLInputElement>('input[aria-label="Message"]')
      ?.focus();
  }, []);

  // F14: visible pending/error state instead of a bare fire-and-forget async call.
  const toggleThreadMutation = useMutation({
    mutationFn: (vars: { threadId: string; nextStatus: "open" | "closed" }) =>
      patchThread(api, vars.threadId, vars.nextStatus),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      setMoreOpen(false);
    },
  });

  // F19: close the "more" menu on outside click and Escape.
  React.useEffect(() => {
    if (!moreOpen) return undefined;
    function onPointerDown(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMoreOpen(false);
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setMoreOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [moreOpen]);

  if (!conversation) {
    return (
      <div
        className={cn(
          "flex h-14 items-center border-b border-neutral-800 bg-neutral-900 px-3 text-sm text-neutral-400",
          className,
        )}
      >
        Select a conversation
      </div>
    );
  }

  const title =
    conversation.contact?.display_name ??
    formatPhone(conversation.contact_e164);

  return (
    <header
      className={cn(
        "flex flex-col border-b border-neutral-800 bg-neutral-900",
        className,
      )}
    >
      <div className="flex items-center justify-between gap-3 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          {onBack && (
            <button
              type="button"
              onClick={onBack}
              aria-label="Back to conversation list"
              className="shrink-0 rounded-md p-2 text-neutral-300 hover:bg-neutral-800 hover:text-neutral-50 md:hidden"
            >
              <ArrowLeft className="h-4 w-4" />
            </button>
          )}
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-neutral-50">{title}</h2>
            <p className="truncate text-[11px] text-neutral-400">
              {formatPhone(conversation.contact_e164)} · via{" "}
              {formatPhone(conversation.our_e164)}
            </p>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          <button
            type="button"
            onClick={startCall}
            disabled={!canSend}
            title={canSend ? undefined : "Read-only inbox — you can view but not call"}
            aria-label={`Call ${title}`}
            className="rounded-md p-2 text-neutral-300 hover:bg-neutral-800 hover:text-neutral-50 disabled:pointer-events-none disabled:opacity-40"
          >
            <Phone className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={focusComposer}
            aria-label={`Message ${title}`}
            className="rounded-md p-2 text-neutral-300 hover:bg-neutral-800 hover:text-neutral-50"
          >
            <MessageSquare className="h-4 w-4" />
          </button>
          <div className="relative" ref={menuRef}>
            <button
              type="button"
              aria-haspopup="menu"
              aria-expanded={moreOpen}
              disabled={toggleThreadMutation.isPending}
              onClick={() => setMoreOpen((v) => !v)}
              className="rounded-md p-2 text-neutral-300 hover:bg-neutral-800 hover:text-neutral-50 disabled:opacity-50"
            >
              {toggleThreadMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <MoreHorizontal className="h-4 w-4" />
              )}
            </button>
            {moreOpen && (
              <div
                role="menu"
                aria-label="Conversation actions"
                className="absolute right-0 top-9 z-20 w-44 rounded-md border border-neutral-700 bg-neutral-800 p-1 shadow-lg"
              >
                <button
                  role="menuitem"
                  type="button"
                  onClick={() =>
                    toggleThreadMutation.mutate({
                      threadId: conversation.thread_id,
                      nextStatus: conversation.status === "closed" ? "open" : "closed",
                    })
                  }
                  disabled={toggleThreadMutation.isPending || !canSend}
                  title={canSend ? undefined : "Read-only inbox — you can view but not close or reopen"}
                  className="block w-full rounded px-2 py-1 text-left text-xs text-neutral-200 hover:bg-neutral-700 disabled:opacity-50"
                >
                  {conversation.status === "closed" ? "Reopen" : "Close"}
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
      {toggleThreadMutation.isError && (
        <p role="alert" className="px-3 pb-2 text-[11px] text-red-400">
          {(toggleThreadMutation.error as Error).message}
        </p>
      )}
    </header>
  );
}
