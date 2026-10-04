import * as React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
import { useCampaigns, useReleaseNumber } from "@/api/hooks";
import { fetchInboxes, patchInbox, type Inbox } from "@/api/conversations";
import { answeredBy, useNumbers, type NumberOut } from "@/api/numbers";
import { CONSOLE_PANEL as PANEL } from "@/components/ui/consoleChrome";
import {
  Button,
  Input,
  MutationStatus,
  Pill,
  Spinner,
  TabPanel,
  Tabs,
  panelId,
  tabId,
  type PillTone,
} from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { cn } from "@/lib/utils";
import { EmergencyAddressEditor } from "@/pages/NumbersPage";
import { InboxGrantEditor } from "@/pages/InboxSettingsPage";
import {
  AnsweredByControl,
  CampaignPicker,
  lineStatus,
  type CampaignWithCap,
} from "@/pages/LinesPage";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "access", label: "Who can use it" },
  { id: "calls", label: "Calls" },
  { id: "texting", label: "Texting" },
  { id: "advanced", label: "Advanced" },
];
const TABS_ID = "line-detail";

function emergencyLabel(n: NumberOut): { label: string; tone: PillTone } {
  if (!n.e911_supported || n.emergency_status === "unsupported")
    return { label: "Managed automatically", tone: "neutral" };
  switch (n.emergency_status) {
    case "active":
      return { label: "Address on file", tone: "success" };
    case "provisioning":
    case "pending":
      return { label: "Setting up", tone: "warning" };
    case "off":
      return { label: "911 not switched on", tone: "neutral" };
    default:
      return { label: "No 911 address", tone: "danger" };
  }
}

function registrationTone(r: string): PillTone {
  return r === "approved" ? "success" : r === "pending" ? "warning" : r === "rejected" ? "danger" : "neutral";
}

function InfoCard({ title, children, onOpen, openLabel }: {
  title: string;
  children: React.ReactNode;
  onOpen?: () => void;
  openLabel?: string;
}) {
  return (
    <div className={cn(PANEL, "space-y-2 p-3.5")}>
      <p className="text-[11px] font-semibold uppercase tracking-[0.07em] text-[hsl(var(--cx-muted))]">{title}</p>
      <div className="text-sm text-foreground">{children}</div>
      {onOpen && (
        <Button type="button" size="sm" variant="outline" className="rounded-full px-3" onClick={onOpen}>
          {openLabel ?? "Change"}
        </Button>
      )}
    </div>
  );
}

function TargetsForm({ inbox }: { inbox: Inbox }) {
  const { api } = useAuth();
  const qc = useQueryClient();
  const [first, setFirst] = React.useState(
    inbox.sla_first_response_minutes == null ? "" : String(inbox.sla_first_response_minutes),
  );
  const [resolve, setResolve] = React.useState(
    inbox.sla_resolution_minutes == null ? "" : String(inbox.sla_resolution_minutes),
  );
  const [invalid, setInvalid] = React.useState(false);
  const save = useMutation({
    mutationFn: (data: Parameters<typeof patchInbox>[2]) => patchInbox(api, inbox.id, data),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["inboxes"] }),
  });
  function parse(v: string): number | "clear" | "invalid" {
    const t = v.trim();
    if (t === "") return "clear";
    const n = Number(t);
    return Number.isInteger(n) && n >= 1 && n <= 44640 ? n : "invalid";
  }
  function submit() {
    const f = parse(first);
    const r = parse(resolve);
    if (f === "invalid" || r === "invalid") {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    const data: Parameters<typeof patchInbox>[2] = {};
    if (f === "clear") data.clear_sla_first_response = true;
    else data.sla_first_response_minutes = f;
    if (r === "clear") data.clear_sla_resolution = true;
    else data.sla_resolution_minutes = r;
    save.mutate(data);
  }
  return (
    <div className={cn(PANEL, "space-y-3 p-3.5")}>
      <p className="text-sm font-semibold text-foreground">Reply and resolve targets</p>
      <div className="flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          First reply within (minutes)
          <Input type="number" min={1} max={44640} className="h-9 w-24" value={first}
            aria-label="First reply within (minutes)" onChange={(e) => setFirst(e.target.value)} />
        </label>
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          Resolve within (minutes)
          <Input type="number" min={1} max={44640} className="h-9 w-24" value={resolve}
            aria-label="Resolve within (minutes)" onChange={(e) => setResolve(e.target.value)} />
        </label>
        <Button type="button" className="rounded-full px-5" disabled={save.isPending} onClick={submit}>Save</Button>
        <MutationStatus pending={save.isPending} error={save.error} pendingLabel="Saving…" />
      </div>
      {invalid && <p role="alert" className="text-xs text-destructive">Enter a whole number of minutes, or leave it blank.</p>}
      <p className="text-xs text-muted-foreground">Leave a box blank to stop tracking that time.</p>
    </div>
  );
}

function AdvancedTab({ number, inbox, onReleased }: { number: NumberOut; inbox: Inbox | null; onReleased: () => void }) {
  const { api } = useAuth();
  const qc = useQueryClient();
  const gate = useGate();
  const [name, setName] = React.useState(inbox?.name ?? "");
  const [color, setColor] = React.useState(inbox?.color ?? "#3b82f6");
  const rename = useMutation({
    mutationFn: () => patchInbox(api, inbox!.id, { name, color }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["inboxes"] }),
  });
  const release = useReleaseNumber(api);
  const [confirming, setConfirming] = React.useState(false);
  const isAdmin = inbox?.my_role === "admin";
  return (
    <div className="space-y-4">
      {inbox && isAdmin && (
        <div className={cn(PANEL, "space-y-3 p-3.5")}>
          <p className="text-sm font-semibold text-foreground">Name and colour</p>
          <div className="flex flex-wrap items-center gap-3">
            <Input aria-label="Line name" className="w-56" value={name} onChange={(e) => setName(e.target.value)} />
            <Input aria-label="Line colour" type="color" className="h-9 w-16 p-1" value={color}
              onChange={(e) => setColor(e.target.value)} />
            <Button type="button" className="rounded-full px-5" disabled={rename.isPending || !name.trim()}
              onClick={() => rename.mutate()}>Save</Button>
            <MutationStatus pending={rename.isPending} error={rename.error} pendingLabel="Saving…" />
          </div>
          <p className="text-xs text-muted-foreground">
            Departments are set under Who can use it: a department that holds access shows this line in its group.
          </p>
        </div>
      )}
      {number.carrier_campaign_locked && number.campaign_id && (
        <div className={cn(PANEL, "p-3.5 text-sm text-muted-foreground")}>
          This number's texting campaign is registered with the phone network. You can move it
          to another campaign, but you can't remove it from here.
        </div>
      )}
      {gate.can("numbers:manage") && number.status !== "released" && (
        <div className={cn(PANEL, "space-y-2 p-3.5")}>
          <p className="text-sm font-semibold text-foreground">Release this number</p>
          <p className="text-xs text-muted-foreground">The number stops working and can't be undone.</p>
          <div className="flex items-center gap-3">
            <Button type="button" size="sm" className="rounded-full px-4"
              variant={confirming ? "destructive" : "outline"} disabled={release.isPending}
              onClick={async () => {
                if (!confirming) return setConfirming(true);
                try {
                  await release.mutateAsync(number.id);
                  onReleased();
                } catch {
                  /* shown below */
                }
              }}>
              {confirming ? "Confirm release" : "Release"}
            </Button>
            <MutationStatus pending={release.isPending} error={release.error} pendingLabel="Saving…" />
          </div>
        </div>
      )}
    </div>
  );
}

export function LineDetailPage({ numberId }: { numberId: string }) {
  const { theme } = useSurfaceTheme();
  const { api } = useAuth();
  const gate = useGate();
  const [searchParams, setSearchParams] = useSearchParams();
  const numbersQuery = useNumbers(api);
  const inboxesQuery = useQuery({ queryKey: ["inboxes"], queryFn: () => fetchInboxes(api) });
  const campaignsQuery = useCampaigns(api);
  const [editing911, setEditing911] = React.useState(false);

  const param = searchParams.get("tab");
  const tab = TABS.some((t) => t.id === param) ? (param as string) : "overview";
  function setTab(id: string) {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("tab", id);
        return next;
      },
      { replace: true },
    );
  }
  function back() {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("line");
      next.delete("tab");
      return next;
    });
  }

  const wrap = (children: React.ReactNode) => (
    <div className={cn(surfaceThemeClass(theme), "mx-auto max-w-5xl space-y-5 bg-background p-6 text-foreground")}>
      <button type="button" className="text-sm font-semibold text-primary underline" onClick={back}>
        ← All lines
      </button>
      {children}
    </div>
  );

  if (numbersQuery.isLoading) return wrap(<Spinner />);
  const number = (numbersQuery.data ?? []).find((n) => n.id === numberId);
  if (!number) return wrap(<p role="alert" className="text-sm text-destructive">This line was not found.</p>);

  const inbox = (inboxesQuery.data ?? []).find((i) => i.number_id === number.id) ?? null;
  const campaigns = (campaignsQuery.data ?? []) as CampaignWithCap[];
  const status = lineStatus(number);
  const e911 = emergencyLabel(number);
  const current = answeredBy(number);
  const depts = inbox?.departments ?? [];
  const canManage911 = gate.can("numbers:manage") && number.e911_supported && number.status !== "released";

  const body = (() => {
    switch (tab) {
      case "access":
        return inbox && inbox.my_role === "admin" ? (
          <InboxGrantEditor inbox={inbox} />
        ) : (
          <p className="text-sm text-muted-foreground">Only line admins can change who can use this line.</p>
        );
      case "calls":
        return (
          <div className={cn(PANEL, "space-y-2 p-3.5")}>
            <p className="text-sm font-semibold text-foreground">Who answers</p>
            <AnsweredByControl number={number} />
            <p className="text-xs text-muted-foreground">
              Ring order, queues and call flows live under <Link to="/settings/calling" className="underline">Calling</Link>.
            </p>
          </div>
        );
      case "texting":
        return (
          <div className="space-y-4">
            <div className={cn(PANEL, "space-y-2 p-3.5")}>
              <p className="text-sm font-semibold text-foreground">Registration</p>
              <Pill tone={registrationTone(number.registration)}>{number.registration}</Pill>
              <p className="pt-2 text-sm font-semibold text-foreground">10DLC campaign</p>
              <CampaignPicker number={number} campaigns={campaigns} />
            </div>
            {inbox && inbox.my_role === "admin" && <TargetsForm inbox={inbox} />}
          </div>
        );
      case "advanced":
        return <AdvancedTab number={number} inbox={inbox} onReleased={back} />;
      default:
        return (
          <div className="grid gap-3 sm:grid-cols-2">
            <InfoCard title="Texting" onOpen={() => setTab("texting")}>
              <Pill tone={registrationTone(number.registration)}>{number.registration}</Pill>
            </InfoCard>
            <InfoCard title="911" onOpen={canManage911 ? () => setEditing911(true) : undefined}
              openLabel={number.emergency_address_id ? "Change address" : "Set 911 address"}>
              <Pill tone={e911.tone}>{e911.label}</Pill>
              {editing911 && (
                <div className="mt-3">
                  <EmergencyAddressEditor api={api} number={number} onDone={() => setEditing911(false)} />
                </div>
              )}
            </InfoCard>
            <InfoCard title="Answered by" onOpen={() => setTab("calls")}>
              {current.mode === "assistant" ? "An assistant" : "A person"}
            </InfoCard>
            <InfoCard title="Who can use it" onOpen={() => setTab("access")}>
              {depts.length > 0 ? depts.map((d) => d.name).join(", ") : "No department"}
            </InfoCard>
          </div>
        );
    }
  })();

  return wrap(
    <>
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-[19px] font-semibold tracking-[-0.015em]">{formatPhone(number.e164)}</h2>
        {inbox && <span className="text-sm text-muted-foreground">{inbox.name}</span>}
        <Pill tone={status.tone}>{status.label}</Pill>
      </div>
      <Tabs tabs={TABS} value={tab} onChange={setTab} ariaLabel="Line sections" id={TABS_ID} />
      {TABS.map((t) =>
        t.id === tab ? (
          <TabPanel key={t.id} tabsId={TABS_ID} id={t.id}>
            {body}
          </TabPanel>
        ) : (
          <div key={t.id} role="tabpanel" hidden id={panelId(TABS_ID, t.id)} aria-labelledby={tabId(TABS_ID, t.id)} />
        ),
      )}
    </>,
  );
}
