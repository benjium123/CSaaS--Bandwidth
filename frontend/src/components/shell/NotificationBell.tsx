import { Bell } from "lucide-react";
import * as React from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import {
  fetchNotifications,
  isNotificationCreated,
  markNotificationsRead,
  notificationKindLabel,
  type Notification,
} from "@/api/inboxPro";
import { useOptionalSoftphone } from "@/softphone/SoftphoneProvider";
import { relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Badge, Button, MutationStatus } from "@/components/ui/primitives";

const ORDERED_KINDS = ["mention", "assignment", "overdue", "missed_call"] as const;

type NotificationGroup = {
  heading: string;
  items: Notification[];
};

function newestFirst(a: Notification, b: Notification): number {
  return Date.parse(b.created_at) - Date.parse(a.created_at);
}

function groupNotifications(items: Notification[]): NotificationGroup[] {
  const byHeading = new Map<string, Notification[]>();

  for (const item of items) {
    const heading = notificationKindLabel(item.kind);
    const list = byHeading.get(heading) ?? [];
    list.push(item);
    byHeading.set(heading, list);
  }

  const ordered: NotificationGroup[] = [];
  for (const kind of ORDERED_KINDS) {
    const heading = notificationKindLabel(kind);
    const list = byHeading.get(heading);
    if (list) ordered.push({ heading, items: [...list].sort(newestFirst) });
  }

  const remaining = Array.from(byHeading.entries())
    .filter(([heading]) => !ordered.some((group) => group.heading === heading))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([heading, list]) => ({ heading, items: [...list].sort(newestFirst) }));

  return [...ordered, ...remaining];
}

export function NotificationBell() {
  const { api } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = React.useState(false);
  const wrapperRef = React.useRef<HTMLDivElement | null>(null);

  const notificationsQuery = useQuery({
    queryKey: ["notifications"],
    queryFn: () => fetchNotifications(api, { limit: 50 }),
    staleTime: 15000,
    refetchInterval: 60000,
  });

  const notifications = notificationsQuery.data?.items ?? [];
  const unreadCount = notificationsQuery.data?.unread_count ?? 0;
  const hasUnread = unreadCount > 0;
  const triggerName = hasUnread ? `Alerts, ${unreadCount} unread` : "Alerts";

  const markAllMutation = useMutation({
    mutationFn: () => markNotificationsRead(api, { all: true }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const markOneMutation = useMutation({
    mutationFn: (id: string) => markNotificationsRead(api, { ids: [id] }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const softphone = useOptionalSoftphone();
  React.useEffect(() => {
    if (!softphone) return;
    return softphone.subscribe((event) => {
      const maybeNotification = event as Record<string, unknown> & { type?: string };
      if (isNotificationCreated(maybeNotification)) {
        void queryClient.invalidateQueries({ queryKey: ["notifications"] });
      }
    });
  }, [softphone, queryClient]);

  React.useEffect(() => {
    if (!open) return;

    function onMouseDown(event: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }

    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={wrapperRef} className="relative">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label={triggerName}
        aria-haspopup="menu"
        aria-expanded={open}
        className="relative"
        onClick={() => setOpen((next) => !next)}
      >
        <Bell className="h-5 w-5" aria-hidden="true" />
        {hasUnread ? (
          <Badge
            aria-hidden="true"
            className="absolute -right-1 -top-1 bg-primary text-primary-foreground"
          >
            {unreadCount > 9 ? "9+" : String(unreadCount)}
          </Badge>
        ) : null}
      </Button>

      {open ? (
        <div
          role="menu"
          aria-label="Alerts"
          className="absolute left-11 top-0 z-50 w-80 rounded-md border border-border bg-background p-1 shadow-lg"
        >
          <div className="flex items-center justify-between px-3 py-2">
            <p className="text-xs font-medium text-foreground">Alerts</p>
            {hasUnread ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => markAllMutation.mutate()}
                disabled={markAllMutation.isPending}
              >
                Mark all as read
              </Button>
            ) : null}
          </div>

          <MutationStatus
            pending={markAllMutation.isPending}
            error={markAllMutation.isError ? markAllMutation.error : undefined}
            className="px-3 py-1 text-xs"
          />
          <MutationStatus
            pending={markOneMutation.isPending}
            error={markOneMutation.isError ? markOneMutation.error : undefined}
            className="px-3 py-1 text-xs"
          />

          {notifications.length === 0 ? (
            <p className="px-3 py-2 text-sm text-muted-foreground">Nothing new.</p>
          ) : (
            <div className="max-h-80 overflow-y-auto">
              {groupNotifications(notifications).map((group) => (
                // role="group" + aria-label, not a bare heading: inside a menu, an
                // element that is neither a group nor a menuitem is ignored, so a
                // screen-reader user would hear the alerts with no idea which kind
                // they were. The visible heading is hidden from the tree because the
                // group's own name already says it.
                <div
                  key={group.heading}
                  role="group"
                  aria-label={group.heading}
                  className="mb-1"
                >
                  <p
                    aria-hidden="true"
                    className="px-3 py-1 text-xs font-medium text-muted-foreground"
                  >
                    {group.heading}
                  </p>
                  {group.items.map((item) => (
                    <Button
                      key={item.id}
                      type="button"
                      variant="ghost"
                      role="menuitem"
                      className={cn(
                        "flex w-full items-start justify-start gap-2 rounded-md px-3 py-2 text-left",
                        item.read_at ? "" : "bg-muted",
                      )}
                      onClick={() => {
                        markOneMutation.mutate(item.id);
                        setOpen(false);
                        // A conversation is addressed by (our_e164, contact_e164), not by
                        // thread_id (P20c) - services/notifications.py now stamps both
                        // alongside thread_id, so the bell can navigate straight to it.
                        if (item.our_e164 && item.contact_e164) {
                          const params = new URLSearchParams({
                            contact: item.contact_e164,
                            our: item.our_e164,
                          });
                          navigate(`/inbox?${params.toString()}`);
                        }
                      }}
                    >
                      <span className="flex-1 whitespace-pre-wrap">{item.body}</span>
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {relativeTime(item.created_at)}
                      </span>
                      {item.read_at ? null : <span className="sr-only">Unread</span>}
                    </Button>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
