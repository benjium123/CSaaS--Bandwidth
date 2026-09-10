import * as React from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Loader2, MessageSquare, MoreHorizontal, Phone, Star } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { useSoftphone } from "@/softphone/SoftphoneProvider";
import {
  patchThread,
  putImportantPair,
  type Conversation,
  type CursorPage,
} from "@/api/conversations";
import { Button } from "@/components/ui/primitives";
import { PhoneNumberMenu } from "@/components/ui/PhoneNumberMenu";
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

  // Item: star toggle (POST /api/v1/inbox/important-pair). Optimistically flips
  // `important` on this pair everywhere it appears in the conversations cache - the list
  // row's star and this header both read off that same cache, so both update together
  // without waiting on the round trip - then reverts on error and reconciles with the
  // server on settle.
  type ConversationsCache = { pages: CursorPage<Conversation>[]; pageParams: unknown[] };
  const importantMutation = useMutation({
    mutationFn: (vars: { ourE164: string; contactE164: string; important: boolean }) =>
      putImportantPair(api, vars.ourE164, vars.contactE164, vars.important),
    onMutate: async (vars) => {
      await queryClient.cancelQueries({ queryKey: ["conversations"] });
      const previous = queryClient.getQueriesData<ConversationsCache>({
        queryKey: ["conversations"],
      });
      queryClient.setQueriesData<ConversationsCache>(
        { queryKey: ["conversations"] },
        (data) => {
          // Defensive: setQueriesData matches by key PREFIX, so any future query keyed
          // under ["conversations", ...] that is not an infinite query would land here.
          // Returning it untouched is always right; mapping .pages on it would throw
          // inside onMutate and kill the mutation before mutationFn ever ran.
          if (!data || !Array.isArray(data.pages)) return data;
          return {
            ...data,
            pages: data.pages.map((page) => ({
              ...page,
              items: page.items.map((item) =>
                item.our_e164 === vars.ourE164 && item.contact_e164 === vars.contactE164
                  ? { ...item, important: vars.important }
                  : item,
              ),
            })),
          };
        },
      );
      return { previous };
    },
    onError: (_err, _vars, context) => {
      context?.previous.forEach(([key, data]) => {
        queryClient.setQueryData(key, data);
      });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });

  const toggleImportant = React.useCallback(() => {
    if (!conversation || !canSend) return;
    importantMutation.mutate({
      ourE164: conversation.our_e164,
      contactE164: conversation.contact_e164,
      important: !conversation.important,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation, canSend]);

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
          "flex h-14 items-center border-b border-border bg-background px-3 text-sm text-muted-foreground",
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
        "flex flex-col border-b border-border bg-background",
        className,
      )}
    >
      <div className="flex items-center justify-between gap-3 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          {onBack && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={onBack}
              aria-label="Back to conversation list"
              className="shrink-0 text-foreground hover:bg-muted hover:text-foreground md:hidden"
            >
              <ArrowLeft className="h-4 w-4" />
            </Button>
          )}
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <h2 className="truncate text-sm font-semibold text-foreground">{title}</h2>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={toggleImportant}
                disabled={!canSend || importantMutation.isPending}
                aria-pressed={Boolean(conversation.important)}
                title={
                  canSend
                    ? conversation.important
                      ? "Unmark as important"
                      : "Mark as important"
                    : "Read-only inbox — you can view but not mark important"
                }
                aria-label={conversation.important ? "Unmark as important" : "Mark as important"}
                // size="icon" is a 36px box, which would double the height of this title
                // row - the star sits INSIDE the heading line, so it keeps its old 0.5 padding.
                className="h-auto w-auto shrink-0 rounded-md p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
              >
                <Star
                  className={cn(
                    "h-3.5 w-3.5",
                    conversation.important && "fill-amber-400 text-amber-400",
                  )}
                />
              </Button>
            </div>
            {/* A <div>, not the old <p>: PhoneNumberMenu renders a positioned <div> wrapper
                for its pop-up menu, and a <div> inside a <p> is invalid HTML - the browser
                closes the paragraph early and the layout breaks. OUR number is plain text:
                you cannot text or call yourself. */}
            <div className="flex min-w-0 items-center gap-1 text-[11px] text-muted-foreground">
              <PhoneNumberMenu
                e164={conversation.contact_e164}
                fromE164={conversation.our_e164}
                onText={focusComposer}
                disabled={!canSend}
                disabledReason="Read-only inbox — you can view but not call"
                className="h-auto min-w-0 px-0 py-0 text-[11px] font-normal text-muted-foreground hover:text-foreground"
              />
              <span className="shrink-0">· via</span>
              <span className="truncate">{formatPhone(conversation.our_e164)}</span>
            </div>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={startCall}
            disabled={!canSend}
            title={canSend ? undefined : "Read-only inbox — you can view but not call"}
            aria-label={`Call ${title}`}
            className="text-foreground hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
          >
            <Phone className="h-4 w-4" />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={focusComposer}
            aria-label={`Message ${title}`}
            className="text-foreground hover:bg-muted hover:text-foreground"
          >
            <MessageSquare className="h-4 w-4" />
          </Button>
          <div className="relative" ref={menuRef}>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-haspopup="menu"
              aria-expanded={moreOpen}
              // P20b: an icon-only trigger with no accessible name was already an a11y
              // hole; it became ambiguous too once the subtitle gained its own
              // aria-expanded PhoneNumberMenu trigger. Name it.
              aria-label="Conversation actions"
              disabled={toggleThreadMutation.isPending}
              onClick={() => setMoreOpen((v) => !v)}
              className="text-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
            >
              {toggleThreadMutation.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <MoreHorizontal className="h-4 w-4" />
              )}
            </Button>
            {moreOpen && (
              <div
                role="menu"
                aria-label="Conversation actions"
                className="absolute right-0 top-9 z-20 w-44 rounded-md border border-border bg-muted p-1 shadow-lg"
              >
                <Button
                  type="button"
                  role="menuitem"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    const threadId = conversation.thread_id;
                    if (!threadId) return;
                    toggleThreadMutation.mutate({
                      threadId,
                      nextStatus: conversation.status === "closed" ? "open" : "closed",
                    });
                  }}
                  disabled={toggleThreadMutation.isPending || !canSend || !conversation.thread_id}
                  title={
                    !conversation.thread_id
                      ? "This conversation has no messages yet — nothing to close or reopen"
                      : canSend
                        ? undefined
                        : "Read-only inbox — you can view but not close or reopen"
                  }
                  className="w-full justify-start rounded px-2 py-1 text-left text-xs text-foreground hover:bg-foreground/10 disabled:opacity-50"
                >
                  {conversation.status === "closed" ? "Reopen" : "Close"}
                </Button>
              </div>
            )}
          </div>
        </div>
      </div>
      {toggleThreadMutation.isError && (
        <p role="alert" className="px-3 pb-2 text-[11px] text-destructive">
          {(toggleThreadMutation.error as Error).message}
        </p>
      )}
      {importantMutation.isError && (
        <p role="alert" className="px-3 pb-2 text-[11px] text-destructive">
          {(importantMutation.error as Error).message}
        </p>
      )}
    </header>
  );
}
