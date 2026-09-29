import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { useAuth } from "@/auth/AuthContext";
import { getDesktopBridge, type DesktopNotice, type RingliteDesktop } from "@/lib/desktopBridge";

/**
 * Operator alerting for the website support inbox (routes/site.py).
 *
 * Deliberately does NOT touch the desktop badge: DesktopAlerts owns that, and two components
 * writing the same badge would fight over the number.
 */

export interface UnreadChat {
  id: string;
  name: string;
  kind: "customer" | "visitor";
  last_visitor_at: string | null;
}

export interface UnreadChatsResponse {
  count: number;
  chats: UnreadChat[];
}

/** Shared by every component that watches the operator inbox. */
export const SUPPORT_UNREAD_KEY = ["ops", "site-chats-unread"] as const;
export const SUPPORT_UNREAD_PATH = "/api/v1/ops/site/chats/unread";

export function supportChatRoute(id: string): string {
  return "/ops?section=website&chat=" + id;
}

/**
 * The first successful poll only records what is already waiting - no historical alert storm.
 * Every later poll reports the chats whose `last_visitor_at` moved, plus any chat id it has
 * never seen before.
 */
export function diffUnread(
  prev: Map<string, string> | null,
  chats: UnreadChat[],
): { next: Map<string, string>; fresh: UnreadChat[] } {
  const next = new Map<string, string>();
  for (const chat of chats) next.set(chat.id, chat.last_visitor_at ?? "");
  if (prev === null) return { next, fresh: [] };

  const fresh: UnreadChat[] = [];
  for (const chat of chats) {
    const before = prev.get(chat.id);
    if (before !== undefined && before === (chat.last_visitor_at ?? "")) continue;
    fresh.push(chat);
  }
  return { next, fresh };
}

function beep(): void {
  if (typeof AudioContext === "undefined") return;
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = 880;
    gain.gain.value = 0.08;
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.15);
  } catch {
    // A blocked or unsupported AudioContext must never break the poll.
  }
}

function chatNotice(chat: UnreadChat): DesktopNotice {
  return {
    title: "Support chat: " + chat.name,
    body: chat.kind === "customer" ? "Customer is waiting" : "Website visitor is waiting",
    tag: "support:" + chat.id,
    route: supportChatRoute(chat.id),
  };
}

function summaryNotice(count: number): DesktopNotice {
  return {
    title: "Support chat",
    body: count + " support chats need a reply",
    tag: "support:summary",
    route: "/ops?section=website",
  };
}

/**
 * The desktop shell prefers the bridge notification, because it survives a backgrounded tab.
 * The browser Notification API is the fallback and only fires while the tab is hidden, so a
 * visible console is never interrupted by a toast it did not ask for.
 */
function announce(bridge: RingliteDesktop | null, notice: DesktopNotice, navigate: (to: string) => void): void {
  if (bridge) {
    try {
      bridge.notify(notice);
    } catch {
      // The bridge is best effort: a shell failure must never break the console.
    }
    return;
  }
  if (typeof Notification === "undefined") return;
  if (Notification.permission !== "granted") return;
  if (!document.hidden) return;
  try {
    const shown = new Notification(notice.title, { body: notice.body, tag: notice.tag });
    shown.onclick = () => {
      window.focus();
      if (notice.route) navigate(notice.route);
    };
  } catch {
    // Notifications are best effort too.
  }
}

/**
 * Invisible shell component: alerts an operator who is looking somewhere else. In a browser
 * without a bridge or without notification permission every path above degrades to the beep.
 */
export function SupportAlerts(): null {
  const { api, me } = useAuth();
  const navigate = useNavigate();
  const operator = Boolean(me?.is_platform_operator);
  const seen = useRef<Map<string, string> | null>(null);

  const query = useQuery({
    queryKey: SUPPORT_UNREAD_KEY,
    queryFn: () => api.request<UnreadChatsResponse>(SUPPORT_UNREAD_PATH),
    enabled: operator,
    refetchInterval: 10000,
    refetchIntervalInBackground: true,
    staleTime: 0,
  });

  useEffect(() => {
    if (!operator) return;
    const data = query.data;
    if (!data) return;

    const { next, fresh } = diffUnread(seen.current, data.chats);
    seen.current = next;
    if (fresh.length === 0) return;

    // A burst is capped, and the third slot becomes one summary rather than a flood.
    const bridge = getDesktopBridge();
    const shown = fresh.length <= 3 ? fresh : fresh.slice(0, 2);
    for (const chat of shown) {
      beep();
      announce(bridge, chatNotice(chat), navigate);
    }
    if (fresh.length > 3) {
      beep();
      announce(bridge, summaryNotice(fresh.length), navigate);
    }
  }, [navigate, operator, query.data]);

  return null;
}
