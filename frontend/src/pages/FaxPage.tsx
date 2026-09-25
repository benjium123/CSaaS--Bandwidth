import * as React from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { ApiError, type ApiClient } from "@/api/client";
import {
  downloadFaxDocument,
  faxFileRejectionReason,
  useFaxes,
  useSendFax,
  useSetFaxMode,
  type FaxNumberOut,
  type FaxOut,
} from "@/api/fax";
import { formatMicros } from "@/api/spend";
import {
  CONSOLE_CELL as CELL,
  CONSOLE_CELL_L as CELL_L,
  CONSOLE_CELL_R as CELL_R,
  CONSOLE_HEAD as HEAD,
  CONSOLE_PANEL as PANEL,
  CONSOLE_ROW as ROW,
  CONSOLE_TABLE as TABLE,
  PageHeader,
} from "@/components/ui/consoleChrome";
import {
  Button,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Spinner,
  mutationErrorMessage,
  type PillTone,
} from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { cn } from "@/lib/utils";

function statusPill(status: string): { label: string; tone: PillTone } {
  switch (status) {
    case "delivered":
    case "received":
      return { label: status === "delivered" ? "Delivered" : "Received", tone: "success" };
    case "failed":
      return { label: "Failed", tone: "danger" };
    case "queued":
      return { label: "Queued", tone: "warning" };
    case "sending":
      return { label: "Sending", tone: "warning" };
    case "receiving":
      return { label: "Receiving", tone: "warning" };
    default:
      return { label: status, tone: "neutral" };
  }
}

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** The one place that turns a failed send into the sentence the operator asked for: the
 * server's own message for everything except 402, where the plain "Request failed with
 * 402" would otherwise leak through. */
function sendErrorMessage(err: unknown): { message: string; isInsufficientCredits: boolean } {
  if (err instanceof ApiError && err.status === 402) {
    return { message: "Add credits to send faxes", isInsufficientCredits: true };
  }
  return { message: mutationErrorMessage(err), isInsufficientCredits: false };
}

export function FaxPage() {
  // The console follows the one stored theme preference the front door writes. See
  // src/auth/useSurfaceTheme.ts.
  const { theme } = useSurfaceTheme();
  const { api } = useAuth();
  const { data, isLoading, isError, error, refetch } = useFaxes(api);

  return (
    <div className={cn(surfaceThemeClass(theme), "mx-auto max-w-5xl space-y-8 bg-background p-6 text-foreground")}>
      <PageHeader title="Fax" description="Send and receive faxes on your Telnyx numbers." />

      {isLoading ? (
        <Spinner />
      ) : isError ? (
        <div role="alert" className="flex items-center gap-3 text-sm text-destructive">
          <span>{mutationErrorMessage(error)}</span>
          <Button type="button" size="sm" className="rounded-full px-3.5" variant="outline" onClick={() => refetch()}>
            Retry
          </Button>
        </div>
      ) : (
        <>
          <SendFaxCard api={api} faxNumbers={data?.fax_numbers ?? []} />
          <FaxLinesCard api={api} numbers={data?.numbers ?? []} />
          <FaxHistorySection faxes={data?.faxes ?? []} api={api} />
        </>
      )}
    </div>
  );
}

function SendFaxCard({ api, faxNumbers }: { api: ApiClient; faxNumbers: string[] }) {
  const sendFax = useSendFax(api);
  const [from, setFrom] = React.useState("");
  const [to, setTo] = React.useState("");
  const [file, setFile] = React.useState<File | null>(null);
  const [fileError, setFileError] = React.useState<string | null>(null);
  const [sent, setSent] = React.useState(false);
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);

  React.useEffect(() => {
    if (!from && faxNumbers.length > 0) setFrom(faxNumbers[0]);
  }, [faxNumbers, from]);

  function onFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = e.target.files?.[0] ?? null;
    setFileError(null);
    setSent(false);
    if (picked) {
      const reason = faxFileRejectionReason(picked);
      if (reason) {
        setFileError(reason);
        setFile(null);
        e.target.value = "";
        return;
      }
    }
    setFile(picked);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSent(false);
    if (!file) {
      setFileError("Attach a PDF or TIFF.");
      return;
    }
    try {
      await sendFax.mutateAsync({ from, to, file });
      setSent(true);
      setTo("");
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    } catch {
      // surfaced via the error readout below
    }
  }

  const { message: sendError, isInsufficientCredits } = sendFax.error
    ? sendErrorMessage(sendFax.error)
    : { message: "", isInsufficientCredits: false };

  return (
    <Section title="Send a fax" description="$0.10 per page, sent and received.">
      {faxNumbers.length === 0 ? (
        <EmptyState
          title="No fax numbers yet"
          description="Turn on fax for a number below."
        />
      ) : (
        <form className={cn(PANEL, "flex flex-wrap items-end gap-3 p-3.5")} onSubmit={submit}>
          <div className="space-y-1.5">
            <label className="block text-xs text-muted-foreground" htmlFor="fax-from">
              From
            </label>
            <Select id="fax-from" aria-label="From" value={from} onChange={(e) => setFrom(e.target.value)}>
              {faxNumbers.map((e164) => (
                <option key={e164} value={e164}>
                  {formatPhone(e164)}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1.5">
            <label className="block text-xs text-muted-foreground" htmlFor="fax-to">
              To
            </label>
            <Input
              id="fax-to"
              aria-label="To"
              placeholder="+12145550100"
              value={to}
              onChange={(e) => setTo(e.target.value)}
              required
            />
          </div>
          <div className="space-y-1.5">
            <label className="block text-xs text-muted-foreground" htmlFor="fax-file">
              Document
            </label>
            <input
              id="fax-file"
              ref={fileInputRef}
              aria-label="Document"
              type="file"
              accept=".pdf,.tif,.tiff"
              onChange={onFileChange}
              className="block text-xs text-foreground file:mr-3 file:rounded-full file:border-0 file:bg-primary file:px-3.5 file:py-1.5 file:text-xs file:font-medium file:text-primary-foreground"
            />
          </div>
          <Button type="submit" className="rounded-full px-5" disabled={sendFax.isPending}>
            {sendFax.isPending ? "Sending…" : "Send"}
          </Button>

          {fileError && (
            <p role="alert" className="w-full text-sm text-destructive">
              {fileError}
            </p>
          )}
          {sendFax.isError && (
            <p role="alert" className="w-full text-sm text-destructive">
              {sendError}
              {isInsufficientCredits && (
                <>
                  {" "}
                  <Link to="/settings/billing" className="underline">
                    Add credits
                  </Link>
                </>
              )}
            </p>
          )}
          {sent && !sendFax.isError && (
            <p role="status" className="w-full text-sm text-muted-foreground">
              Fax sent.
            </p>
          )}
        </form>
      )}
    </Section>
  );
}

function FaxLinesCard({ api, numbers }: { api: ApiClient; numbers: FaxNumberOut[] }) {
  const setFaxMode = useSetFaxMode(api);

  return (
    <Section
      title="Fax lines"
      description="A fax line does not take voice calls; texting keeps working."
    >
      {numbers.length === 0 ? (
        <EmptyState title="No numbers yet" description="Order a number to send or receive faxes." />
      ) : (
        <div className={cn(PANEL, "overflow-x-auto")}>
          <table className={TABLE}>
            <thead>
              <tr>
                <th className={HEAD}>Number</th>
                <th className={HEAD}>Carrier</th>
                <th className={HEAD}>Fax mode</th>
              </tr>
            </thead>
            <tbody>
              {numbers.map((n) => (
                <tr key={n.id} className={ROW}>
                  <td className={cn(CELL_L, "whitespace-nowrap font-semibold text-foreground")}>
                    {formatPhone(n.e164)}
                  </td>
                  <td className={cn(CELL, "text-muted-foreground")}>{n.carrier}</td>
                  <td className={CELL_R}>
                    <Button
                      type="button"
                      role="switch"
                      aria-checked={n.fax_mode}
                      aria-label={`Fax mode for ${n.e164}`}
                      variant={n.fax_mode ? "default" : "outline"}
                      size="sm"
                      className="rounded-full px-3.5"
                      disabled={setFaxMode.isPending && setFaxMode.variables?.numberId === n.id}
                      onClick={() => setFaxMode.mutate({ numberId: n.id, enabled: !n.fax_mode })}
                    >
                      {n.fax_mode ? "On" : "Off"}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <MutationStatus pending={setFaxMode.isPending} error={setFaxMode.error} pendingLabel="Saving…" />
    </Section>
  );
}

function FaxHistorySection({ faxes, api }: { faxes: FaxOut[]; api: ApiClient }) {
  return (
    <Section title="History">
      {faxes.length === 0 ? (
        <EmptyState title="No faxes yet" description="A fax you send or receive shows up here." />
      ) : (
        <div className={cn(PANEL, "overflow-x-auto")}>
          <table className={TABLE}>
            <thead>
              <tr>
                <th className={HEAD}>Direction</th>
                <th className={HEAD}>From</th>
                <th className={HEAD}>To</th>
                <th className={HEAD}>Pages</th>
                <th className={HEAD}>Status</th>
                <th className={HEAD}>Cost</th>
                <th className={HEAD}>Time</th>
                <th className={HEAD}>Document</th>
              </tr>
            </thead>
            <tbody>
              {faxes.map((f) => (
                <FaxRow key={f.id} fax={f} api={api} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function FaxRow({ fax, api }: { fax: FaxOut; api: ApiClient }) {
  const [downloading, setDownloading] = React.useState(false);
  const [downloadError, setDownloadError] = React.useState<string | null>(null);
  const status = statusPill(fax.status);

  async function download() {
    setDownloadError(null);
    setDownloading(true);
    try {
      await downloadFaxDocument(api, fax.id, fax.document_name || "fax.pdf");
    } catch (err) {
      setDownloadError(mutationErrorMessage(err));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <tr className={ROW}>
      <td className={cn(CELL_L, "text-muted-foreground")}>
        {fax.direction === "inbound" ? "Received" : "Sent"}
      </td>
      <td className={cn(CELL, "whitespace-nowrap text-foreground")}>{formatPhone(fax.from)}</td>
      <td className={cn(CELL, "whitespace-nowrap text-foreground")}>{formatPhone(fax.to)}</td>
      <td className={cn(CELL, "text-muted-foreground")}>{fax.pages ?? "—"}</td>
      <td className={CELL}>
        <Pill tone={status.tone}>{status.label}</Pill>
        {fax.status === "failed" && fax.failure_reason && (
          <span className="mt-1 block max-w-[220px] text-xs text-destructive">{fax.failure_reason}</span>
        )}
      </td>
      <td className={cn(CELL, "whitespace-nowrap text-foreground")}>{formatMicros(fax.charged_micros)}</td>
      <td className={cn(CELL, "whitespace-nowrap text-muted-foreground")}>
        {formatWhen(fax.completed_at ?? fax.created_at)}
      </td>
      <td className={CELL_R}>
        {fax.has_document ? (
          <>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="rounded-full px-3.5"
              disabled={downloading}
              onClick={download}
            >
              {downloading ? "Downloading…" : "Download"}
            </Button>
            {downloadError && (
              <span className="mt-1 block max-w-[180px] text-xs text-destructive">{downloadError}</span>
            )}
          </>
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        )}
      </td>
    </tr>
  );
}
