import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { useInbox, useTags, type InboxFilters } from "@/api/hooks";
import { ThreadList } from "@/components/inbox/ThreadList";
import { ThreadView } from "@/components/inbox/ThreadView";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

const STATUS_TABS = [
  { key: "open", label: "Open" },
  { key: "closed", label: "Closed" },
  { key: "", label: "All" },
];

const ASSIGNED_TABS = [
  { key: "", label: "Everyone" },
  { key: "me", label: "Mine" },
  { key: "unassigned", label: "Unassigned" },
];

export function InboxPage() {
  const { api } = useAuth();
  const [status, setStatus] = React.useState("open");
  const [assigned, setAssigned] = React.useState("");
  const [q, setQ] = React.useState("");
  const [labelId, setLabelId] = React.useState("");
  const [selectedId, setSelectedId] = React.useState<string | null>(null);

  const filters: InboxFilters = React.useMemo(
    () => ({
      status: status || undefined,
      assigned: assigned || undefined,
      q: q.trim() || undefined,
      label_id: labelId || undefined,
    }),
    [status, assigned, q, labelId],
  );

  const { data, isLoading, error } = useInbox(api, filters);
  const { data: tags } = useTags(api);
  const items = data?.items ?? [];
  const selected = items.find((i) => i.thread.id === selectedId) ?? null;

  return (
    <div className="grid h-full grid-cols-[minmax(280px,360px)_1fr]">
      <aside className="flex min-h-0 flex-col border-r border-border">
        <div className="space-y-2 border-b border-border p-3">
          <Input
            aria-label="Search conversations"
            placeholder="Search name or number"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <div className="flex gap-1" role="tablist" aria-label="Status">
            {STATUS_TABS.map((t) => (
              <Button
                key={t.label}
                role="tab"
                aria-selected={status === t.key}
                size="sm"
                variant={status === t.key ? "default" : "ghost"}
                onClick={() => setStatus(t.key)}
              >
                {t.label}
              </Button>
            ))}
          </div>
          <div className="flex gap-1" role="tablist" aria-label="Assignment">
            {ASSIGNED_TABS.map((t) => (
              <Button
                key={t.label}
                role="tab"
                aria-selected={assigned === t.key}
                size="sm"
                variant={assigned === t.key ? "default" : "ghost"}
                onClick={() => setAssigned(t.key)}
              >
                {t.label}
              </Button>
            ))}
          </div>
          {tags && tags.length > 0 && (
            <select
              aria-label="Filter by label"
              className={cn(
                "h-8 w-full rounded-md border border-border bg-background px-2 text-xs",
              )}
              value={labelId}
              onChange={(e) => setLabelId(e.target.value)}
            >
              <option value="">All labels</option>
              {tags.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {isLoading ? (
            <Spinner label="Loading inbox" />
          ) : error ? (
            <p role="alert" className="p-4 text-sm text-destructive">
              {(error as Error).message}
            </p>
          ) : (
            <ThreadList items={items} selectedId={selectedId} onSelect={setSelectedId} />
          )}
        </div>
      </aside>

      <section className="min-h-0">
        <ThreadView api={api} item={selected} />
      </section>
    </div>
  );
}
