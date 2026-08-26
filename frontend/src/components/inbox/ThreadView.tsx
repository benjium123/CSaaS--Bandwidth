import * as React from "react";
import type { ApiClient } from "@/api/client";
import {
  useMarkRead,
  usePatchThread,
  useSendMessage,
  useThreadMessages,
  type InboxItem,
} from "@/api/hooks";
import { Button, Spinner } from "@/components/ui/primitives";
import { formatPhone, relativeTime, statusTick } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Composer } from "./Composer";

export function MessageBubble({
  direction,
  body,
  status,
  errorCode,
  createdAt,
}: {
  direction: string;
  body: string | null;
  status: string;
  errorCode: string | null;
  createdAt: string;
}) {
  const outbound = direction === "outbound";
  const tick = statusTick(status);
  return (
    <li className={cn("flex", outbound ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[75%] space-y-1 rounded-lg px-3 py-2 text-sm",
          outbound ? "bg-primary text-primary-foreground" : "bg-muted",
        )}
      >
        <p className="whitespace-pre-wrap break-words">{body}</p>
        <div className="flex items-center justify-end gap-2 text-[11px] opacity-80">
          <span>{relativeTime(createdAt)}</span>
          {outbound && (
            <span title={tick.label} aria-label={tick.label}>
              {tick.glyph}
            </span>
          )}
          {tick.bad && errorCode && (
            <span
              className="rounded bg-destructive px-1 text-white"
              title={`Carrier error ${errorCode}`}
            >
              {errorCode}
            </span>
          )}
        </div>
      </div>
    </li>
  );
}

export function ThreadView({ api, item }: { api: ApiClient; item: InboxItem | null }) {
  const threadId = item?.thread.id ?? null;
  const { data: messages, isLoading } = useThreadMessages(api, threadId);
  const send = useSendMessage(api);
  const markRead = useMarkRead(api);
  const patch = usePatchThread(api);

  // Opening a thread marks it read. Keyed on the id so re-polls do not re-fire it.
  React.useEffect(() => {
    if (threadId) markRead.mutate(threadId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);

  if (!item) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        Select a conversation
      </div>
    );
  }

  const title = item.contact?.display_name ?? formatPhone(item.thread.contact_e164);
  const closed = item.thread.status === "closed";

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between gap-3 border-b border-border p-3">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">{title}</h2>
          <p className="text-[11px] text-muted-foreground">
            {formatPhone(item.thread.contact_e164)} &middot; via{" "}
            {formatPhone(item.thread.our_e164)}
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() =>
            patch.mutate({ threadId: item.thread.id, status: closed ? "open" : "closed" })
          }
        >
          {closed ? "Reopen" : "Close"}
        </Button>
      </header>

      <div className="flex-1 overflow-y-auto p-3">
        {isLoading ? (
          <Spinner label="Loading messages" />
        ) : (
          <ul className="space-y-2">
            {(messages ?? []).map((m) => (
              <MessageBubble
                key={m.id}
                direction={m.direction}
                body={m.body}
                status={m.status}
                errorCode={m.error_code}
                createdAt={m.created_at}
              />
            ))}
          </ul>
        )}
      </div>

      <Composer
        onSend={async (body, allowReassign) => {
          await send.mutateAsync({
            to: item.thread.contact_e164,
            body,
            allow_reassign: allowReassign,
          });
        }}
      />
    </div>
  );
}
