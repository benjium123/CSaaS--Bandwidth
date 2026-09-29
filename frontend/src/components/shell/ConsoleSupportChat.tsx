/**
 * The console's "Chat with us": the same assistant the marketing site runs, opened from the
 * Help menu's first item.
 *
 * It is deliberately inert until someone asks for it. The widget starts a live-chat poll once a
 * customer has been handed over, and no console page should pay for that on every load - so the
 * component mounts the widget on the first open and then leaves it mounted, which is what keeps a
 * reply that arrives after the panel is closed waiting when it is opened again.
 *
 * The theme class matters here: this node renders OUTSIDE the Shell's `console-surface` wrapper
 * (it is a sibling of the shell, not a child), so it carries the console tokens itself for the
 * `.rl-console-chat` custom properties that supportChat.css defines.
 */
import * as React from "react";

import { useAuth } from "@/auth/AuthContext";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { ChatWidget } from "@/marketing/ChatWidget";
import { cn } from "@/lib/utils";

import "./supportChat.css";

/** Fired on `window` to ask the console to open the support chat. */
export const OPEN_SUPPORT_CHAT_EVENT = "ringlite:open-support-chat";

/** Ask the console to open the support chat, from anywhere. */
export function openSupportChat(): void {
  window.dispatchEvent(new Event(OPEN_SUPPORT_CHAT_EVENT));
}

export function ConsoleSupportChat(): JSX.Element | null {
  const { me, orgId } = useAuth();
  const { theme } = useSurfaceTheme();
  const [open, setOpen] = React.useState(false);
  const [mounted, setMounted] = React.useState(false);

  React.useEffect(() => {
    const onOpen = () => {
      setMounted(true);
      setOpen(true);
    };
    window.addEventListener(OPEN_SUPPORT_CHAT_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_SUPPORT_CHAT_EVENT, onOpen);
  }, []);

  // Signed out (or a workspace-free operator console): there is nobody to chat AS.
  if (!me) return null;
  // Lazy: nothing renders until the first open, and stays rendered after it.
  if (!mounted) return null;

  const email = (me.email ?? "").trim();
  const name = me.full_name?.trim() || email.split("@")[0] || "";
  const membership = me.memberships.find((m) => m.org_id === orgId);
  const context = membership?.org_name;

  return (
    <div className={cn("console-surface", surfaceThemeClass(theme), "rl-console-chat")}>
      <ChatWidget
        variant="console"
        open={open}
        onOpenChange={setOpen}
        identity={context ? { name, email, context } : { name, email }}
      />
    </div>
  );
}
