import * as React from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { PanelRight } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import {
  fetchConversations,
  fetchInboxes,
  fetchUnreadByInbox,
  type ConversationFilter,
  type ConversationTab,
} from "@/api/conversations";
import { useSoftphone } from "@/softphone/SoftphoneProvider";
import { ConversationList } from "@/components/conversations/ConversationList";
import { Timeline } from "@/components/conversations/Timeline";
import { ConversationHeader } from "@/components/conversations/ConversationHeader";
import { ContactPanel } from "@/components/conversations/ContactPanel";
import {
  NewConversationPanel,
  type FromOption,
  type NewConversationKind,
} from "@/components/conversations/NewConversationPanel";
import { Composer } from "@/components/inbox/Composer";
import { InboxColumn, type InboxColumnSelection } from "@/components/conversations/InboxColumn";
import { ScheduledDrawer } from "@/components/conversations/ScheduledDrawer";
import { Button, Sheet } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

/** Item 2: the list is kept fresh two ways - a background poll while the tab is visible
 * (TanStack Query pauses `refetchInterval` in the background by default, so this alone
 * already satisfies "when visible"), PLUS an immediate invalidate the moment a
 * `message.received` event arrives over the realtime socket, so a new message shows up
 * without waiting out the rest of the poll interval. */
const CONVERSATIONS_POLL_MS = 5000;

/** F20: debounce the search box before it enters a query key - typing shouldn't refetch
 * on every keystroke. Returns the debounced value; the caller keeps the raw value for the
 * input itself so it stays responsive. */
function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = React.useState(value);
  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = React.useState(false);

  React.useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mql = window.matchMedia(query);
    const update = () => setMatches(mql.matches);
    update();
    mql.addEventListener("change", update);
    return () => mql.removeEventListener("change", update);
  }, [query]);

  return matches;
}

const ALL_INBOXES = "all";

/** ConversationsPage renders inside the app Shell (frontend/src/App.tsx), which already
 * mounts the one persistent <Sidebar /> for the whole authed app - this page owns only
 * the list / timeline / contact-panel columns to its right, never its own Sidebar copy. */
export function ConversationsPage() {
  const { api, orgId } = useAuth();
  const softphone = useSoftphone();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { inboxId: routeInboxId } = useParams<{ inboxId?: string; threadId?: string }>();
  const [searchParams, setSearchParams] = useSearchParams();

  const [tab, setTab] = React.useState<ConversationTab>("chats");
  const [filter, setFilter] = React.useState<ConversationFilter>("open");
  const [q, setQ] = React.useState("");
  const debouncedQ = useDebouncedValue(q, 300);
  const [contactPanelOpen, setContactPanelOpen] = React.useState(false);
  const [mobileInboxSheetOpen, setMobileInboxSheetOpen] = React.useState(false);
  // P28: the send-later list. Local state, not a URL param - it is a peek at a queue, not
  // a place you would link somebody to.
  const [scheduledOpen, setScheduledOpen] = React.useState(false);
  // Item 1: "+ New" compose mode - independent of the URL-selected conversation, so
  // Cancel can return to whatever was selected before without losing/mangling it.
  const [composeMode, setComposeMode] = React.useState<NewConversationKind | null>(null);
  /** Seed for a compose opened from another page via `/inbox?compose=<e164>[&from=<our>]`.
   *  Cleared as soon as compose mode ends, so a later "+ New" starts blank. */
  const [composeSeed, setComposeSeed] = React.useState<{ to: string; from: string | null } | null>(
    null,
  );

  // `?compose=` is consumed once and stripped from the URL (replace, so Back doesn't
  // re-open it): the panel owns the value from here on, and a refresh should not
  // resurrect a half-typed message.
  React.useEffect(() => {
    const to = searchParams.get("compose");
    if (!to) return;
    setComposeSeed({ to, from: searchParams.get("from") });
    setComposeMode("message");
    const next = new URLSearchParams(searchParams);
    next.delete("compose");
    next.delete("from");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const isBelowSm = useMediaQuery("(max-width: 639px)");

  const inboxesQuery = useQuery({
    queryKey: ["inboxes"],
    queryFn: () => fetchInboxes(api),
    staleTime: 1000,
  });
  const inboxes = inboxesQuery.data ?? [];

  // Unread counts for the InboxColumn are decorative. This query must never gate/block
  // anything and its failure must never surface as a page-level error - while it loads
  // or after it fails, the column simply shows no counts.
  //
  // The key is DELIBERATELY outside the ["conversations"] family. ConversationHeader's
  // optimistic star toggle does setQueriesData({queryKey:["conversations"]}, d => ...
  // d.pages.map(...)), so anything sharing that prefix but not shaped like an infinite
  // query would throw inside onMutate and kill the mutation before it ever fired. The
  // message.received effect below invalidates this key explicitly instead.
  const unreadQuery = useQuery({
    queryKey: ["inbox-unread-counts"],
    queryFn: () => fetchUnreadByInbox(api),
    staleTime: 5000,
    refetchInterval: 15000,
  });

  const urlInboxId = searchParams.get("inbox");
  const requestedInboxId = routeInboxId ?? urlInboxId;
  // F7: ?inbox=all is an explicit "every inbox I can see" mode, distinct from "no inbox
  // chosen yet" - it must never be overwritten by the auto-select-first effect below.
  const isAllInboxes = requestedInboxId === ALL_INBOXES;
  const selectedInboxId = React.useMemo(() => {
    if (isAllInboxes) return null;
    if (requestedInboxId && inboxes.some((inbox) => inbox.id === requestedInboxId)) {
      return requestedInboxId;
    }
    return inboxes[0]?.id ?? null;
  }, [isAllInboxes, requestedInboxId, inboxes]);

  React.useEffect(() => {
    if (routeInboxId) return;
    if (isAllInboxes) return;
    if (!searchParams.has("inbox") && inboxes.length > 0) {
      const next = new URLSearchParams(searchParams);
      next.set("inbox", inboxes[0].id);
      setSearchParams(next, { replace: true });
    }
  }, [isAllInboxes, inboxes, searchParams, setSearchParams, routeInboxId]);

  const conversationsQuery = useInfiniteQuery({
    queryKey: ["conversations", isAllInboxes ? ALL_INBOXES : selectedInboxId, tab, filter, debouncedQ],
    queryFn: ({ pageParam }) =>
      fetchConversations(api, {
        inbox_id: isAllInboxes ? undefined : (selectedInboxId ?? undefined),
        tab,
        filter,
        q: debouncedQ.trim() || undefined,
        cursor: pageParam as string | undefined,
      }),
    enabled: isAllInboxes || Boolean(selectedInboxId),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    // Item 2: TanStack Query pauses refetchInterval while the tab is in the background
    // by default (refetchIntervalInBackground defaults to false), so this alone already
    // covers "poll only when visible" - no manual visibility check needed.
    refetchInterval: CONVERSATIONS_POLL_MS,
  });

  const items = conversationsQuery.data?.pages.flatMap((page) => page.items) ?? [];

  // Item 2: a new inbound/outbound message anywhere should refresh the list right away
  // rather than waiting out the rest of the poll interval.
  React.useEffect(() => {
    return softphone.subscribe((event) => {
      if (event.type !== "message.received") return;
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["inbox-unread-counts"] });
    });
  }, [softphone, queryClient]);

  // Item 34: a stale ?contact/?our from the previous org must not leak into the newly
  // selected org's view (wrong conversation, or a 404/403 if the ids don't even exist
  // there).
  const prevOrgIdRef = React.useRef(orgId);
  React.useEffect(() => {
    if (prevOrgIdRef.current === orgId) return;
    prevOrgIdRef.current = orgId;
    if (!searchParams.has("contact") && !searchParams.has("our")) return;
    const next = new URLSearchParams(searchParams);
    next.delete("contact");
    next.delete("our");
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId]);

  const urlContact = searchParams.get("contact");
  const urlOur = searchParams.get("our");

  // :threadId is intentionally not used to select a conversation. A conversation is
  // the pair (our_e164, contact_e164) and thread_id is null for call-only pairs, so it
  // cannot address every conversation. Keep ?contact= / ?our= as the thread selector.
  const selectedConversation = React.useMemo(
    () => items.find((item) => item.contact_e164 === urlContact) ?? null,
    [items, urlContact],
  );
  // F13: the conversation's own our_e164 is authoritative once it's loaded - ?our is only
  // an initial seed so the timeline can start fetching before the list has resolved.
  const ourE164 =
    selectedConversation?.our_e164 ??
    urlOur ??
    inboxes.find((inbox) => inbox.id === selectedInboxId)?.e164 ??
    null;

  // The inbox that actually governs the SELECTED conversation - not just the sidebar's
  // current filter - so viewer-gating (F1/F2) and the "shared with" panel stay correct
  // in ?inbox=all mode, where the sidebar has no single active inbox.
  const activeInbox =
    inboxes.find((inbox) => inbox.id === selectedConversation?.inbox_id) ??
    inboxes.find((inbox) => inbox.id === selectedInboxId) ??
    null;
  // T8: the only gate on this surface stays the per-inbox `my_role !== "viewer"` check
  // (`canSend` / `canCompose`). Do NOT add useCapabilities / useGate here. The backend
  // stays the authority; this page only decides what to render.
  // canSend defaults to false until we actually know the answer (inboxes still loading,
  // or the conversation resolved before its inbox did) - true only once we positively
  // know either the inbox role allows it, or there is no inbox system at all to gate on
  // (a legacy/no-inbox org: inboxes finished loading and there are none).
  const canSend = activeInbox
    ? activeInbox.my_role !== "viewer"
    : !inboxesQuery.isLoading && inboxes.length === 0;

  // Item 1: the "From" choices for a brand-new conversation - every number the user can
  // actually send from, i.e. every inbox where they aren't a viewer (the same access
  // check canSend above uses for an already-selected conversation), deduped by e164 since
  // more than one inbox can share a number. Empty means "nothing to send from", which
  // gates the "+ New" button off exactly like the Composer/Call button are gated.
  const fromOptions = React.useMemo<FromOption[]>(() => {
    const seen = new Set<string>();
    const options: FromOption[] = [];
    for (const inbox of inboxes) {
      if (inbox.my_role === "viewer" || seen.has(inbox.e164)) continue;
      seen.add(inbox.e164);
      options.push({ e164: inbox.e164, label: inbox.name });
    }
    return options;
  }, [inboxes]);
  const canCompose = fromOptions.length > 0;

  const inboxSelection = React.useMemo<InboxColumnSelection>(() => {
    if (
      filter === "important" ||
      filter === "unresponded" ||
      filter === "snoozed" ||
      filter === "overdue"
    ) {
      return { kind: "view", view: filter };
    }
    if (isAllInboxes) return { kind: "all" };
    if (selectedInboxId) return { kind: "inbox", inboxId: selectedInboxId };
    return { kind: "all" };
  }, [filter, isAllInboxes, selectedInboxId]);

  const scopeLabel = React.useMemo(() => {
    if (filter === "important") return "Important";
    if (filter === "unresponded") return "Unresponded";
    if (filter === "snoozed") return "Snoozed";
    if (filter === "overdue") return "Overdue";
    if (isAllInboxes) return "All conversations";
    if (selectedInboxId) {
      return inboxes.find((inbox) => inbox.id === selectedInboxId)?.name ?? "All conversations";
    }
    return "All conversations";
  }, [filter, isAllInboxes, selectedInboxId, inboxes]);

  function handleInboxSelect(selection: InboxColumnSelection) {
    setMobileInboxSheetOpen(false);
    if (selection.kind === "view") {
      setFilter((current) => (current === selection.view ? "open" : selection.view));
      return;
    }

    const pathname = selection.kind === "all" ? "/inbox/all" : `/inbox/${selection.inboxId}`;
    const next = new URLSearchParams(searchParams);
    next.delete("inbox");
    const search = next.toString();
    navigate({ pathname, search: search ? `?${search}` : "" });
  }

  const sendMessage = useMutation({
    // P28: media_ids / scheduled_for / track_links come straight from the composer's
    // extras and are posted verbatim - the page adds nothing and validates nothing here,
    // because the composer already refused a past time and an oversized attachment.
    mutationFn: async (vars: {
      to: string;
      body: string;
      allow_reassign: boolean;
      from: string;
      media_ids?: string[];
      scheduled_for?: string;
      track_links?: boolean;
    }) => api.request("/api/v1/messages", { method: "POST", json: vars }),
    // Item 10: invalidate off the MUTATION'S OWN variables, not the closed-over
    // selectedConversation/ourE164 - the user can switch to a different conversation
    // while this send is still in flight, and onSuccess would otherwise invalidate the
    // NEW selection's timeline (wrong query) while leaving the thread that actually
    // just got a new message stale.
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: ["timeline", variables.to, variables.from],
      });
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });

  function handleSelect(contactE164: string) {
    setComposeSeed(null);
    setComposeMode(null);
    const conversation = items.find((item) => item.contact_e164 === contactE164);
    const next = new URLSearchParams(searchParams);
    next.set("contact", contactE164);
    if (conversation) next.set("our", conversation.our_e164);
    setSearchParams(next);
    // Below sm the contact panel is a MODAL bottom sheet (aria-modal + focus trap), so
    // auto-opening it on every row click would trap the user the instant they pick a
    // conversation. On sm and up it is an ordinary side panel and may open eagerly.
    if (!isBelowSm) setContactPanelOpen(true);
  }

  /** Below md the conversation list and the selected conversation share one column
   * (F-follow-up 2) - this is the way back from the detail view to the list. */
  function handleBack() {
    const next = new URLSearchParams(searchParams);
    next.delete("contact");
    next.delete("our");
    setSearchParams(next);
  }

  // Item 1: select the (our_e164, contact_e164) pair a "New text message"/"New call"
  // just created/dialed, the same way clicking an existing row would - the timeline then
  // picks it up on its own via ?contact/?our, even before the conversations list poll has
  // caught up with the brand-new pair.
  function selectPair(contactE164: string, ourE164: string) {
    const next = new URLSearchParams(searchParams);
    next.set("contact", contactE164);
    next.set("our", ourE164);
    setSearchParams(next);
    if (!isBelowSm) setContactPanelOpen(true);
  }

  async function handleComposeSendMessage(vars: {
    from: string;
    to: string;
    body: string;
    allowReassign: boolean;
  }) {
    await sendMessage.mutateAsync({
      to: vars.to,
      from: vars.from,
      body: vars.body,
      allow_reassign: vars.allowReassign,
    });
    setComposeSeed(null);
    setComposeMode(null);
    selectPair(vars.to, vars.from);
  }

  async function handleComposeCall(vars: { from: string; to: string }) {
    await softphone.dial(vars.to, vars.from);
    setComposeSeed(null);
    setComposeMode(null);
    selectPair(vars.to, vars.from);
  }

  const inboxColumnElement = (
    <InboxColumn
      inboxes={inboxes}
      isLoading={inboxesQuery.isLoading}
      error={inboxesQuery.error ? (inboxesQuery.error as Error).message : null}
      selection={inboxSelection}
      onSelect={handleInboxSelect}
      unread={unreadQuery.data?.counts ?? {}}
      unreadTruncated={unreadQuery.data?.truncated ?? false}
      onNew={(kind) => {
        setComposeSeed(null);
        setComposeMode(kind);
      }}
      onOpenScheduled={() => {
        setMobileInboxSheetOpen(false);
        setScheduledOpen(true);
      }}
      canCompose={canCompose}
      canComposeLoading={inboxesQuery.isLoading}
      className={cn("h-full", isBelowSm ? "!w-full border-r-0" : "")}
    />
  );

  const contactPanelElement = (
    <ContactPanel
      conversation={composeMode ? null : selectedConversation}
      inbox={activeInbox}
      canSend={canSend}
      className={cn(
        !isBelowSm && (contactPanelOpen ? "fixed inset-y-0 right-0 z-40 w-80" : "hidden"),
        "lg:static lg:z-auto lg:block lg:w-auto",
      )}
    />
  );

  return (
    <div className="dark grid h-full grid-cols-[minmax(0,1fr)] bg-background text-foreground lg:grid-cols-[220px_minmax(0,1fr)_300px]">
      {isBelowSm ? (
        <>
          <div className="sm:hidden bg-background p-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label="Choose inbox"
              onClick={() => setMobileInboxSheetOpen(true)}
            >
              {scopeLabel}
            </Button>
          </div>

          <Sheet
            open={mobileInboxSheetOpen}
            onClose={() => setMobileInboxSheetOpen(false)}
            side="left"
            title="Inboxes"
          >
            {inboxColumnElement}
          </Sheet>
        </>
      ) : (
        inboxColumnElement
      )}

      <main className="grid min-w-0 grid-cols-[1fr] md:grid-cols-[320px_1fr]">
        <ConversationList
          items={items}
          selectedContactE164={urlContact}
          onSelect={handleSelect}
          tab={tab}
          onTabChange={setTab}
          filter={filter}
          onFilterChange={setFilter}
          q={q}
          onQChange={setQ}
          hasNextPage={conversationsQuery.hasNextPage}
          isFetchingNextPage={conversationsQuery.isFetchingNextPage}
          isLoading={conversationsQuery.isLoading}
          onLoadMore={conversationsQuery.fetchNextPage}
          error={conversationsQuery.error ? (conversationsQuery.error as Error).message : null}
          hasNoInboxAccess={!inboxesQuery.isLoading && inboxes.length === 0}
          className={cn((selectedConversation || composeMode) && "hidden", "md:flex")}
          onNew={(kind) => {
            setComposeSeed(null);
            setComposeMode(kind);
          }}
          canCompose={canCompose}
          canComposeLoading={inboxesQuery.isLoading}
        />

        <section className="flex min-w-0 flex-col bg-background">
          {composeMode ? (
            <NewConversationPanel
              // Load-bearing key: the panel seeds its state on mount, so without a
              // changing key a second `?compose=` for a different number would leave
              // the first number in the To field.
              key={composeSeed ? `compose-${composeSeed.to}` : "compose-new"}
              kind={composeMode}
              fromOptions={fromOptions}
              initialTo={composeSeed?.to ?? null}
              initialFrom={composeSeed?.from ?? null}
              onCancel={() => {
                setComposeSeed(null);
                setComposeMode(null);
              }}
              onSendMessage={handleComposeSendMessage}
              onCall={handleComposeCall}
            />
          ) : (
            <>
              <div className="flex items-center border-b border-border">
                <div className="min-w-0 flex-1">
                  <ConversationHeader
                    conversation={selectedConversation}
                    canSend={canSend}
                    onBack={selectedConversation ? handleBack : undefined}
                  />
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label="Toggle contact panel"
                  onClick={() => setContactPanelOpen((v) => !v)}
                  className="mr-2 text-foreground hover:bg-muted lg:hidden"
                >
                  <PanelRight className="h-4 w-4" />
                </Button>
              </div>

              <Timeline
                contactE164={selectedConversation?.contact_e164 ?? urlContact}
                ourE164={ourE164}
              />

              {selectedConversation && (
                <div>
                  {!canSend && (
                    <p className="border-t border-border px-3 pt-2 text-xs text-muted-foreground">
                      Read-only inbox — you can view but not send
                    </p>
                  )}
                  <Composer
                    // Item 9: a fresh Composer instance per conversation - its internal
                    // draft/busy/error/needsReassign state must never survive a thread
                    // switch (a half-typed reply to Ada must not reappear addressed to Bob).
                    key={selectedConversation.contact_e164}
                    disabled={!canSend}
                    threadId={selectedConversation.thread_id}
                    onNoted={() => {
                      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
                    }}
                    onSend={async (body, allowReassign, extras) => {
                      await sendMessage.mutateAsync({
                        to: selectedConversation.contact_e164,
                        from: selectedConversation.our_e164,
                        body,
                        allow_reassign: allowReassign,
                        ...extras,
                      });
                    }}
                  />
                </div>
              )}
            </>
          )}
        </section>
      </main>

      {isBelowSm ? (
        <Sheet
          open={contactPanelOpen}
          onClose={() => setContactPanelOpen(false)}
          side="bottom"
          title="Contact"
        >
          {contactPanelElement}
        </Sheet>
      ) : (
        contactPanelElement
      )}

      <ScheduledDrawer open={scheduledOpen} onClose={() => setScheduledOpen(false)} />
    </div>
  );
}
