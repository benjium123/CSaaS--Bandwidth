/**
 * The phone dock: the dialer when idle, the live-call controls while a call is up.
 *
 * It owns no call state of its own - the softphone provider does - so it only mirrors the narrow
 * slice it needs (activeCall / muted / onHold and their setters) and keeps the dialer's own input,
 * "from" line, placing flag and error message local. Errors from the dialer and from the in-call
 * controls all land in the same role="alert" so nothing is ever swallowed.
 */
import * as React from "react";
import { Phone } from "lucide-react";
import { Link } from "react-router-dom";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/primitives";
import { useSoftphone } from "@/softphone/SoftphoneProvider";

export type DockFromOption = { e164: string; label: string };

const TICK_MS = 1000;

function formatElapsed(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function PhoneDock(props: {
  fromOptions: DockFromOption[];
  onCall(vars: { to: string; from: string }): Promise<void>;
}): JSX.Element | null {
  const { fromOptions, onCall } = props;
  const { activeCall, muted, onHold, setMuted, setHold, hangUp } = useSoftphone();

  const [to, setTo] = React.useState("");
  const [from, setFrom] = React.useState(fromOptions[0]?.e164 ?? "");
  const [placing, setPlacing] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  // The server refuses calls until the caller has a 911 address on file (e911 gate).
  const [needs911, setNeeds911] = React.useState(false);
  const [elapsed, setElapsed] = React.useState(0);

  const activeId = activeCall?.id ?? null;

  React.useEffect(() => {
    setElapsed(0);
    if (activeId == null) return undefined;

    const tick = window.setInterval(() => {
      setElapsed((seconds) => seconds + 1);
    }, TICK_MS);

    return () => window.clearInterval(tick);
  }, [activeId]);

  const first = fromOptions[0];
  const selectedFrom =
    fromOptions.find((option) => option.e164 === from)?.e164 ?? first?.e164 ?? "";

  const trimmed = to.trim();
  const callDisabled = placing || trimmed.length === 0;

  const run = async (action: () => Promise<void>) => {
    setError(null);
    setNeeds911(false);
    try {
      await action();
    } catch (err) {
      if (err instanceof ApiError && err.code === "e911_address_required") setNeeds911(true);
      setError(messageOf(err));
    }
  };

  const place = async () => {
    if (callDisabled) return;
    setPlacing(true);
    setError(null);
    setNeeds911(false);
    try {
      await onCall({ to: trimmed, from: selectedFrom });
      setTo("");
    } catch (err) {
      if (err instanceof ApiError && err.code === "e911_address_required") setNeeds911(true);
      setError(messageOf(err));
    } finally {
      setPlacing(false);
    }
  };

  if (!activeCall && fromOptions.length === 0) return null;

  return (
    <section aria-label="Phone" className="border-t border-border p-2.5 flex flex-col gap-2">
      {activeCall ? (
        <>
          <div className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-500 text-white"
            >
              <Phone className="h-4 w-4" />
            </span>
            <span className="min-w-0 flex-1 truncate text-sm font-medium">
              {activeCall.contact}
            </span>
            <span
              role="timer"
              aria-label="Call time"
              className="text-sm tabular-nums text-muted-foreground"
            >
              {formatElapsed(elapsed)}
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            <Button
              type="button"
              variant="ghost"
              aria-pressed={muted}
              onClick={() => void run(() => setMuted(!muted))}
            >
              {muted ? "Unmute" : "Mute"}
            </Button>

            <Button
              type="button"
              variant="ghost"
              aria-pressed={onHold}
              onClick={() => void run(() => setHold(!onHold))}
            >
              {onHold ? "Resume" : "Hold"}
            </Button>

            <Button
              type="button"
              variant="destructive"
              className="ml-auto"
              onClick={() => void run(hangUp)}
            >
              End
            </Button>
          </div>
        </>
      ) : (
        <>
          <input
            id="phone-dock-to"
            aria-label="Number to call"
            placeholder="Enter a number"
            inputMode="tel"
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={to}
            onChange={(event) => setTo(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void place();
              }
            }}
          />

          {fromOptions.length > 1 ? (
            <select
              aria-label="Call from"
              className="rounded-md border border-border bg-background px-2 py-1 text-sm"
              value={selectedFrom}
              onChange={(event) => setFrom(event.target.value)}
            >
              {fromOptions.map((option) => (
                <option key={option.e164} value={option.e164}>
                  {`${option.label} · ${option.e164}`}
                </option>
              ))}
            </select>
          ) : (
            <p className="text-xs text-muted-foreground">{`From ${first?.label ?? ""}`}</p>
          )}

          <Button
            type="button"
            aria-label="Call"
            className="gap-1.5"
            disabled={callDisabled}
            onClick={() => void place()}
          >
            <Phone aria-hidden="true" className="h-4 w-4" />
            <span>Call</span>
          </Button>
        </>
      )}

      {needs911 ? (
        <p role="alert" className="text-sm text-destructive">
          Add your 911 address before you call.{" "}
          <Link to="/settings/profile" className="font-medium underline">
            Add it in My profile
          </Link>
        </p>
      ) : error ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}
    </section>
  );
}
