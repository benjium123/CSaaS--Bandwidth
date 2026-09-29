import * as React from "react";
import { CircleHelp } from "lucide-react";
import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";

import { getErrorMessage } from "@/api/contacts";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input, Textarea } from "@/components/ui/primitives";

/** Support/help links exposed by the backend (routes/support.py). Any field may be null. */
export interface SupportContacts {
  email: string | null;
  phone: string | null;
  knowledge_base_url: string | null;
  whats_new_url: string | null;
  status_url: string | null;
  terms_url: string | null;
  privacy_url: string | null;
}

// The project has no vite-env typings; read the build-time version defensively.
const APP_VERSION =
  (import.meta as unknown as { env?: Record<string, string | undefined> }).env?.VITE_APP_VERSION ?? "dev";

const MENU_ITEM = "block w-full rounded-sm px-2 py-1.5 text-left text-sm hover:bg-muted";

/** External links open in a new tab; mailto:/tel: links stay put. */
function externalProps(href: string): { target?: string; rel?: string } {
  return href.startsWith("https://") ? { target: "_blank", rel: "noreferrer" } : {};
}

/**
 * Where the menu opens relative to its button: `rail` (left sidebar, opens up and to the right) or
 * `topbar` (top-right header, opens downward, right-aligned so it never leaves the viewport).
 */
export type HelpMenuPlacement = "rail" | "topbar";

const PLACEMENT_CLASS: Record<HelpMenuPlacement, string> = {
  rail: "bottom-0 left-14",
  topbar: "right-0 top-full mt-2",
};

export function HelpMenu({ placement = "rail" }: { placement?: HelpMenuPlacement } = {}): JSX.Element {
  const { api } = useAuth();
  const [open, setOpen] = React.useState(false);
  const [contactOpen, setContactOpen] = React.useState(false);
  const [subject, setSubject] = React.useState("");
  const [body, setBody] = React.useState("");
  const [sent, setSent] = React.useState(false);
  const wrapper = React.useRef<HTMLDivElement>(null);

  const contacts = useQuery({
    queryKey: ["support", "contacts"],
    queryFn: () => api.request<SupportContacts>("/api/v1/support/contacts"),
    enabled: open,
  });

  const send = useMutation({
    mutationFn: () =>
      api.request("/api/v1/support/requests", {
        method: "POST",
        json: { subject, body, page: window.location.pathname },
      }),
    onSuccess: () => {
      setSent(true);
      setSubject("");
      setBody("");
    },
  });

  const close = React.useCallback(() => {
    setOpen(false);
    setContactOpen(false);
    setSent(false);
  }, []);

  React.useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    const onPointerDown = (event: MouseEvent) => {
      if (wrapper.current && !wrapper.current.contains(event.target as Node)) close();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [open, close]);

  const c = contacts.data;
  const canSend = subject.trim().length > 0 && body.trim().length > 0;

  const openContact = () => {
    send.reset();
    setSent(false);
    setContactOpen(true);
  };

  return (
    <div ref={wrapper} className="relative">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label="Help"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => (open ? close() : setOpen(true))}
      >
        <CircleHelp aria-hidden="true" />
      </Button>

      {open ? (
        <div
          role="menu"
          aria-label="Help"
          className={`absolute ${PLACEMENT_CLASS[placement]} z-50 max-h-[calc(100vh-5rem)] w-64 overflow-y-auto rounded-md border border-border bg-background p-1 text-foreground shadow-lg`}
        >
          <button type="button" role="menuitem" className={MENU_ITEM} onClick={openContact}>
            Contact support
          </button>

          {contactOpen ? (
            <div role="dialog" aria-label="Contact support" className="my-1 space-y-2 border-y border-border px-2 py-2">
              <form
                className="space-y-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (canSend && !send.isPending) send.mutate();
                }}
              >
                <div>
                  <label htmlFor="help-subject" className="block text-xs font-medium">
                    Subject
                  </label>
                  <Input
                    id="help-subject"
                    className="mt-1 w-full"
                    value={subject}
                    onChange={(event) => setSubject(event.target.value)}
                  />
                </div>
                <div>
                  <label htmlFor="help-message" className="block text-xs font-medium">
                    Message
                  </label>
                  <Textarea
                    id="help-message"
                    rows={5}
                    className="mt-1 w-full"
                    value={body}
                    onChange={(event) => setBody(event.target.value)}
                  />
                </div>
                <div className="flex items-center gap-2">
                  <Button type="submit" disabled={!canSend || send.isPending}>
                    {send.isPending ? "Sending…" : "Send"}
                  </Button>
                  <Button type="button" variant="outline" onClick={() => setContactOpen(false)}>
                    Cancel
                  </Button>
                </div>
                {sent ? <p className="text-xs text-green-600">{"Sent - we'll reply by email."}</p> : null}
                {send.isError ? (
                  <p role="alert" className="text-xs text-red-600">
                    {getErrorMessage(send.error)}
                  </p>
                ) : null}
              </form>
            </div>
          ) : null}

          {c?.email ? (
            <a role="menuitem" href={`mailto:${c.email}`} className={MENU_ITEM}>
              Email us
            </a>
          ) : null}

          {c?.phone ? (
            <a role="menuitem" href={`tel:${c.phone}`} className={MENU_ITEM}>
              <span>Call us</span>
              <span className="ml-2 text-xs text-muted-foreground">{c.phone}</span>
            </a>
          ) : null}

          {c?.knowledge_base_url ? (
            <a
              role="menuitem"
              href={c.knowledge_base_url}
              className={MENU_ITEM}
              {...externalProps(c.knowledge_base_url)}
            >
              Knowledge base
            </a>
          ) : null}

          {c?.whats_new_url ? (
            <a role="menuitem" href={c.whats_new_url} className={MENU_ITEM} {...externalProps(c.whats_new_url)}>
              {"What's new"}
            </a>
          ) : null}

          {c?.status_url ? (
            <a role="menuitem" href={c.status_url} className={MENU_ITEM} {...externalProps(c.status_url)}>
              System status
            </a>
          ) : null}

          <Link role="menuitem" to="/settings/profile" className={MENU_ITEM}>
            My profile
          </Link>

          <div role="separator" className="my-1 h-px bg-border" />

          <div className="flex flex-wrap gap-3 px-2 py-1 text-xs">
            {c?.terms_url ? (
              <a href={c.terms_url} className="text-muted-foreground hover:underline" {...externalProps(c.terms_url)}>
                Terms of Service
              </a>
            ) : null}
            {c?.privacy_url ? (
              <a
                href={c.privacy_url}
                className="text-muted-foreground hover:underline"
                {...externalProps(c.privacy_url)}
              >
                Privacy Policy
              </a>
            ) : null}
          </div>

          <p className="px-2 py-1 text-xs text-muted-foreground">Ringlite {APP_VERSION}</p>
        </div>
      ) : null}
    </div>
  );
}
