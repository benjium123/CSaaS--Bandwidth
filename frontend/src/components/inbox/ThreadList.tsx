import type { InboxItem } from "@/api/hooks";
import { Badge } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { formatPhone, relativeTime } from "@/lib/format";

export function ThreadList({
  items,
  selectedId,
  onSelect,
}: {
  items: InboxItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  if (items.length === 0) {
    return <p className="p-4 text-sm text-muted-foreground">No conversations yet.</p>;
  }

  return (
    <ul className="divide-y divide-border" aria-label="Conversations">
      {items.map((item) => {
        const title = item.contact?.display_name ?? formatPhone(item.thread.contact_e164);
        const selected = item.thread.id === selectedId;
        return (
          <li key={item.thread.id}>
            <button
              type="button"
              onClick={() => onSelect(item.thread.id)}
              aria-current={selected ? "true" : undefined}
              className={cn(
                "w-full space-y-1 px-4 py-3 text-left hover:bg-muted",
                selected && "bg-muted",
              )}
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-sm font-medium">{title}</span>
                <span className="shrink-0 text-[11px] text-muted-foreground">
                  {relativeTime(item.thread.last_message_at)}
                </span>
              </div>

              <div className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                  {item.last_message?.body ?? "No messages"}
                </span>
                {item.unread > 0 && (
                  <Badge
                    className="bg-primary text-primary-foreground"
                    aria-label={`${item.unread} unread`}
                  >
                    {item.unread}
                  </Badge>
                )}
              </div>

              {item.labels.length > 0 && (
                <div className="flex flex-wrap gap-1 pt-0.5">
                  {item.labels.map((l) => (
                    <Badge
                      key={l.id}
                      style={{ backgroundColor: `${l.color}22`, color: l.color }}
                    >
                      {l.name}
                    </Badge>
                  ))}
                </div>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
