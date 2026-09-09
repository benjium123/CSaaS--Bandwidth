import * as React from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { PanelRight } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import {
  fetchConversations,
  fetchInboxes,
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

const ALL_INBOXES = "all";

/** ConversationsPage renders inside the app Shell (frontend/src/App.tsx), which already
 * mounts the one persistent <Sidebar /> for the whole authed app - this page owns only
 * the list / timeline / contact-panel columns to its right, never its own Sidebar copy. */
export function ConversationsPage() {
  const { api, orgId } = useAuth();
  const softphone = useSoftphone();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  const [tab, setTab] = React.useState<ConversationTab>("chats");
  const [filter, setFilter] = React.useState<ConversationFilter>("open");
  const [q, setQ] = React.useState("");
  const debouncedQ = useDebouncedValue(q, 300);
  const [contactPanelOpen, setContactPanelOpen] = React.useState(false);
  // Item 1: "+ New" compose mode - independent of the URL-selected conversation, so
  // Cancel can return to whatever was selected before without losing/mangling it.
  const [composeMode, setComposeMode] = React.useState<NewConversationKind | null>(null);

  const inboxesQuery = useQuery({
    queryKey: ["inboxes"],
    queryFn: () => fetchInboxes(api),
    staleTime: 1000,
  });
  const inboxes = inboxesQuery.data ?? [];

  const urlInboxId = searchParams.get("inbox");
  // F7: ?inbox=all is an explicit "every inbox I can see" mode, distinct from "no inbox
  // chosen yet" - it must never be overwritten by the auto-select-first effect below.
  const isAllInboxes = urlInboxId === ALL_INBOXES;
  const selectedInboxId = React.useMemo(() => {
    if (isAllInboxes) return null;
    if (urlInboxId && inboxes.some((inbox) => inbox.id === urlInboxId)) return urlInboxId;
    return inboxes[0]?.id ?? null;
  }, [isAllInboxes, urlInboxId, inboxes]);

  React.useEffect(() => {
    if (isAllInboxes) return;
    if (!searchParams.has("inbox") && inboxes.length > 0) {
      const next = new URLSearchParams(searchParams);
      next.set("inbox", inboxes[0].id);
      setSearchParams(next, { replace: true });
    }
  }, [isAllInboxes, inboxes, searchParams, setSearchParams]);

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

  const sendMessage = useMutation({
    mutationFn: async (vars: { to: string; body: string; allow_reassign: boolean; from: string }) =>
      api.request("/api/v1/messages", { method: "POST", json: vars }),
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
    setComposeMode(null);
    const conversation = items.find((item) => item.contact_e164 === contactE164);
    const next = new URLSearchParams(searchParams);
    next.set("contact", contactE164);
    if (conversation) next.set("our", conversation.our_e164);
    setSearchParams(next);
    setContactPanelOpen(true);
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
    setContactPanelOpen(true);
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
    setComposeMode(null);
    selectPair(vars.to, vars.from);
  }

  async function handleComposeCall(vars: { from: string; to: string }) {
    await softphone.dial(vars.to, vars.from);
    setComposeMode(null);
    selectPair(vars.to, vars.from);
  }

  return (
    <div className="dark grid h-full grid-cols-[minmax(0,1fr)] bg-neutral-950 text-neutral-100 lg:grid-cols-[minmax(0,1fr)_320px]">
      <main className="grid min-w-0 grid-cols-[1fr] md:grid-cols-[minmax(280px,360px)_1fr]">
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
          onNew={(kind) => setComposeMode(kind)}
          canCompose={canCompose}
          canComposeLoading={inboxesQuery.isLoading}
        />

        <section className="flex min-w-0 flex-col bg-neutral-900">
          {composeMode ? (
            <NewConversationPanel
              kind={composeMode}
              fromOptions={fromOptions}
              onCancel={() => setComposeMode(null)}
              onSendMessage={handleComposeSendMessage}
              onCall={handleComposeCall}
            />
          ) : (
            <>
              <div className="flex items-center border-b border-neutral-800">
                <div className="min-w-0 flex-1">
                  <ConversationHeader
                    conversation={selectedConversation}
                    canSend={canSend}
                    onBack={selectedConversation ? handleBack : undefined}
                  />
                </div>
                <button
                  type="button"
                  aria-label="Toggle contact panel"
                  onClick={() => setContactPanelOpen((v) => !v)}
                  className="mr-2 rounded-md p-2 text-neutral-300 hover:bg-neutral-800 lg:hidden"
                >
                  <PanelRight className="h-4 w-4" />
                </button>
              </div>

              <Timeline
                contactE164={selectedConversation?.contact_e164 ?? urlContact}
                ourE164={ourE164}
              />

              {selectedConversation && (
                <div>
                  {!canSend && (
                    <p className="border-t border-neutral-800 px-3 pt-2 text-xs text-neutral-400">
                      Read-only inbox — you can view but not send
                    </p>
                  )}
                  <Composer
                    // Item 9: a fresh Composer instance per conversation - its internal
                    // draft/busy/error/needsReassign state must never survive a thread
                    // switch (a half-typed reply to Ada must not reappear addressed to Bob).
                    key={selectedConversation.contact_e164}
                    disabled={!canSend}
                    onSend={async (body, allowReassign) => {
                      await sendMessage.mutateAsync({
                        to: selectedConversation.contact_e164,
                        from: selectedConversation.our_e164,
                        body,
                        allow_reassign: allowReassign,
                      });
                    }}
                  />
                </div>
              )}
            </>
          )}
        </section>
      </main>

      <ContactPanel
        conversation={composeMode ? null : selectedConversation}
        inbox={activeInbox}
        canSend={canSend}
        className={cn(
          contactPanelOpen ? "fixed inset-y-0 right-0 z-40 w-80" : "hidden",
          "lg:static lg:z-auto lg:block lg:w-auto",
        )}
      />
    </div>
  );
}
