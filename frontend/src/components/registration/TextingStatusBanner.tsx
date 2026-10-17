import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { fetchNotifications, markNotificationsRead } from "@/api/inboxPro";
import { Button } from "@/components/ui/primitives";
import { BANNER_PRIORITY, BannerSlot } from "@/components/shell/BannerSlot";

const WARNING_WORDS = /needs (changes|attention)|could not be added/i;

/**
 * The newest unread texting-registration notice. Shares the bell's ["notifications"] query
 * (same key and options), so there is no second poller; dismissing marks it read.
 */
export function TextingStatusBanner() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const notifications = useQuery({
    queryKey: ["notifications"],
    queryFn: () => fetchNotifications(api, { limit: 50 }),
    staleTime: 15000,
    refetchInterval: 60000,
  });
  const dismiss = useMutation({
    mutationFn: (id: string) => markNotificationsRead(api, { ids: [id] }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const newest = (notifications.data?.items ?? [])
    .filter((n) => n.kind === "texting" && n.read_at === null)
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0];
  if (!newest) return null;

  const warning = WARNING_WORDS.test(newest.body);
  return (
    <BannerSlot priority={BANNER_PRIORITY.texting}>
      <div
        role="status"
        data-tone={warning ? "warning" : "info"}
        className={
          warning
            ? "flex items-center gap-3 border-b border-border bg-destructive/10 px-4 py-1.5"
            : "flex items-center gap-3 border-b border-border bg-muted px-4 py-1.5"
        }
      >
        <p className="min-w-0 flex-1 truncate text-sm">{newest.body}</p>
        <div className="flex shrink-0 items-center gap-1">
          <Link
            to="/settings/messaging"
            className="rounded-md px-2 py-1 text-sm font-medium underline"
          >
            Review
          </Link>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            aria-label="Dismiss"
            onClick={() => dismiss.mutate(newest.id)}
          >
            Dismiss
          </Button>
        </div>
      </div>
    </BannerSlot>
  );
}
