import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import {
  SUPPORT_UNREAD_KEY,
  SUPPORT_UNREAD_PATH,
  type UnreadChatsResponse,
} from "@/components/shell/SupportAlerts";

/** The permission prompt is offered at most once per session - asking on every click is a nag. */
let permissionAsked = false;

function offerNotificationPermission(): void {
  if (permissionAsked) return;
  if (typeof Notification === "undefined") return;
  if (Notification.permission !== "default") return;
  permissionAsked = true;
  try {
    void Notification.requestPermission();
  } catch {
    // Browsers that refuse the call simply keep the console quiet.
  }
}

/** Operator-only shortcut into the support inbox, shown only while chats are waiting. */
export function SupportUnreadPill() {
  const { api, me } = useAuth();
  const operator = Boolean(me?.is_platform_operator);
  const query = useQuery({
    queryKey: SUPPORT_UNREAD_KEY,
    queryFn: () => api.request<UnreadChatsResponse>(SUPPORT_UNREAD_PATH),
    enabled: operator,
    refetchInterval: 10000,
    refetchIntervalInBackground: true,
  });
  const count = query.data?.count ?? 0;
  if (!operator || count <= 0) return null;
  return (
    <Link
      to="/ops?section=website"
      onClick={offerNotificationPermission}
      aria-label={`Support, ${count} waiting`}
      className="inline-flex items-center rounded-full bg-primary px-3 py-1 text-xs font-medium text-primary-foreground hover:opacity-90"
    >
      {`Support · ${count}`}
    </Link>
  );
}
