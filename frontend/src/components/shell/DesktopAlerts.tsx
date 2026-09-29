import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useOptionalSoftphone } from "@/softphone/SoftphoneProvider";
import { fetchConversations, type Conversation } from "@/api/conversations";
import { fetchNotifications, notificationKindLabel, type Notification } from "@/api/inboxPro";
import { formatPhone } from "@/lib/format";
import { getDesktopBridge, type DesktopNotice, type RingliteDesktop } from "@/lib/desktopBridge";

/**
 * The subset of an incoming ring the bridge notification actually needs. Declared structurally
 * (rather than importing the exact `IncomingRing`) so the helpers stay easy to unit test.
 */
export type RingNoticeInput = {
  callId: string;
  from: string;
  kind?: "ring" | "handoff" | "invite";
  summary?: string;
  invite?: { mode: "add" | "transfer"; by: string };
};

export function ringNotice(ring: RingNoticeInput): DesktopNotice {
  if (ring.kind === "invite") {
    return {
      title: ring.invite?.mode === "transfer" ? "Call transfer" : "Join a call",
      body: "From " + (ring.invite?.by || "a teammate"),
      tag: "call:" + ring.callId,
      route: "/inbox",
    };
  }
  if (ring.kind === "handoff") {
    return {
      title: "AI handoff",
      body: ring.summary || formatPhone(ring.from),
      tag: "call:" + ring.callId,
      route: "/inbox",
    };
  }
  return {
    title: "Incoming call",
    body: formatPhone(ring.from),
    tag: "call:" + ring.callId,
    route: "/inbox",
  };
}

function conversationKey(conversation: Conversation): string {
  return conversation.our_e164 + "|" + conversation.contact_e164;
}

export function conversationNotice(conversation: Conversation): DesktopNotice {
  const title = conversation.contact?.display_name || formatPhone(conversation.contact_e164);
  const body =
    conversation.last_event_type === "voicemail"
      ? "New voicemail"
      : conversation.snippet || "New message";
  const route =
    "/inbox?" +
    new URLSearchParams({
      contact: conversation.contact_e164,
      our: conversation.our_e164,
    }).toString();
  return { title, body, tag: "conv:" + conversationKey(conversation), route };
}

export function alertNotice(notification: Notification): DesktopNotice {
  const route =
    notification.our_e164 && notification.contact_e164
      ? "/inbox?" +
        new URLSearchParams({
          contact: notification.contact_e164,
          our: notification.our_e164,
        }).toString()
      : "/inbox";
  return {
    title: notificationKindLabel(notification.kind).replace(/s$/, ""),
    body: notification.body,
    tag: "note:" + notification.id,
    route,
  };
}

/**
 * The desktop shell should never be spammed: at most three notifications per poll. When a burst
 * exceeds that, the third slot is replaced with a single summary notice (n = total new this poll).
 */
export function capNotices(
  list: DesktopNotice[],
  summary: (n: number) => DesktopNotice,
): DesktopNotice[] {
  if (list.length <= 3) return list;
  return [list[0], list[1], summary(list.length)];
}

function safeNotify(bridge: RingliteDesktop, notice: DesktopNotice): void {
  try {
    bridge.notify(notice);
  } catch {
    // The bridge is best effort: a shell failure must never break the console.
  }
}

function safeSetBadge(bridge: RingliteDesktop, count: number): void {
  try {
    bridge.setBadge(count);
  } catch {
    // Ignored for the same reason as safeNotify.
  }
}

/**
 * Invisible shell component: mirrors unread activity onto the OS. Renders nothing. When there is
 * no desktop bridge every query stays disabled and every effect bails out, so a browser tab pays
 * no cost for it.
 */
export function DesktopAlerts(): null {
  const { api } = useAuth();
  const bridge = React.useMemo(() => getDesktopBridge(), []);
  const enabled = bridge != null;
  const softphone = useOptionalSoftphone();
  const incoming = softphone?.incoming;

  const notifiedRings = React.useRef<Set<string>>(new Set());
  const conversationsSeen = React.useRef<Map<string, string> | null>(null);
  const alertsSeen = React.useRef<Set<string> | null>(null);

  // (a) Rings are always urgent, including the ones already ringing on first render.
  React.useEffect(() => {
    if (!bridge || !incoming || incoming.length === 0) return;
    const seen = notifiedRings.current;
    for (const ring of incoming) {
      if (seen.has(ring.callId)) continue;
      seen.add(ring.callId);
      safeNotify(bridge, ringNotice(ring));
    }
  }, [bridge, incoming]);

  // (b) Unread conversations.
  const conversationsQuery = useQuery({
    queryKey: ["desktop-alerts", "unread-conversations"],
    queryFn: () => fetchConversations(api, { filter: "unread" }),
    enabled,
    refetchInterval: 20000,
    refetchIntervalInBackground: true,
    staleTime: 0,
  });

  React.useEffect(() => {
    if (!bridge) return;
    const data = conversationsQuery.data;
    if (!data) return;
    const items = data.items;

    safeSetBadge(bridge, items.length);

    // First successful poll only records what is already there: no historical notification spam.
    if (conversationsSeen.current === null) {
      const primed = new Map<string, string>();
      for (const item of items) primed.set(conversationKey(item), item.last_event_at);
      conversationsSeen.current = primed;
      return;
    }

    const seen = conversationsSeen.current;
    const fresh: Conversation[] = [];
    for (const item of items) {
      const key = conversationKey(item);
      const previous = seen.get(key);
      seen.set(key, item.last_event_at);
      if (item.direction !== "inbound") continue;
      // Missed calls arrive through the notifications feed instead of here.
      if (item.last_event_type === "call") continue;
      if (previous !== undefined && previous === item.last_event_at) continue;
      fresh.push(item);
    }

    const notices = capNotices(fresh.map(conversationNotice), (n) => ({
      title: "Ringlite",
      body: n + " new conversations",
      route: "/inbox?filter=unread",
      tag: "conv:summary",
    }));
    for (const notice of notices) safeNotify(bridge, notice);
  }, [bridge, conversationsQuery.data]);

  // (c) Unread notifications (missed calls, voicemails, …).
  const notificationsQuery = useQuery({
    queryKey: ["desktop-alerts", "unread-notifications"],
    queryFn: () => fetchNotifications(api, { unread: true, limit: 20 }),
    enabled,
    refetchInterval: 30000,
    refetchIntervalInBackground: true,
    staleTime: 0,
  });

  React.useEffect(() => {
    if (!bridge) return;
    const data = notificationsQuery.data;
    if (!data) return;
    const items = data.items;

    if (alertsSeen.current === null) {
      alertsSeen.current = new Set(items.map((item) => item.id));
      return;
    }

    const seen = alertsSeen.current;
    const fresh: Notification[] = [];
    for (const item of items) {
      if (seen.has(item.id)) continue;
      seen.add(item.id);
      fresh.push(item);
    }

    const notices = capNotices(fresh.map(alertNotice), (n) => ({
      title: "Ringlite",
      body: n + " new alerts",
      route: "/inbox",
      tag: "note:summary",
    }));
    for (const notice of notices) safeNotify(bridge, notice);
  }, [bridge, notificationsQuery.data]);

  return null;
}
