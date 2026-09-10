import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Drawer, Spinner } from "@/components/ui/primitives";
import { useAuth } from "@/auth/AuthContext";
import {
  cancelScheduledMessage,
  fetchScheduledMessages,
  type ScheduledMessage,
} from "@/api/messaging";
import { formatPhone } from "@/lib/format";

/**
 * P28: every send-later message that has not gone out yet, across every conversation.
 *
 * The conversation timeline already shows a pending send in its own thread, so this is
 * deliberately the OTHER view: "what have I got queued up?", answered by the one endpoint
 * that can answer it (GET /api/v1/messages?status=scheduled). It is a drawer rather than
 * a fifth inbox view because a scheduled message is not a conversation - it has no
 * unread state, no assignee and no thread of its own to open.
 */
export function ScheduledDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { api } = useAuth();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["scheduled-messages"],
    queryFn: () => fetchScheduledMessages(api),
    // Only while the drawer is open: a background poll for a list nobody is looking at
    // would cost a request every few seconds for the whole session.
    enabled: open,
  });

  const cancel = useMutation({
    mutationFn: (messageId: string) => cancelScheduledMessage(api, messageId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["scheduled-messages"] });
      // The same message is a bubble in its own thread - that view has to lose it too.
      void queryClient.invalidateQueries({ queryKey: ["timeline"] });
    },
  });

  const items: ScheduledMessage[] = query.data ?? [];

  return (
    <Drawer open={open} onClose={onClose} title="Scheduled">
      {query.isLoading ? (
        <Spinner label="Loading scheduled messages" />
      ) : query.error ? (
        <p role="alert" className="text-sm text-destructive">
          {(query.error as Error).message}
        </p>
      ) : items.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing is waiting to go out.</p>
      ) : (
        <ul className="space-y-2">
          {items.map((message) => (
            <li
              key={message.id}
              className="space-y-1 rounded-md border border-border p-2 text-sm"
            >
              <div className="flex items-center gap-2">
                <span className="font-medium">{formatPhone(message.to_e164)}</span>
                <span className="ml-auto text-[11px] text-muted-foreground">
                  {message.scheduled_for
                    ? new Date(message.scheduled_for).toLocaleString()
                    : ""}
                </span>
              </div>
              {message.body && (
                <p className="line-clamp-3 whitespace-pre-wrap break-words text-xs text-muted-foreground">
                  {message.body}
                </p>
              )}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-label={`Cancel message to ${formatPhone(message.to_e164)}`}
                disabled={cancel.isPending}
                onClick={() => cancel.mutate(message.id)}
              >
                Cancel
              </Button>
            </li>
          ))}
        </ul>
      )}
      {cancel.isError && (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {(cancel.error as Error).message}
        </p>
      )}
    </Drawer>
  );
}
