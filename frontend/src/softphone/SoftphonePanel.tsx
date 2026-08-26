/**
 * The app-wide softphone dock (plan phase-6-plan.md deliverable 4). Fixed bottom-right,
 * visible on every authed page. Pure UI over useSoftphone() - all LiveKit/WS state lives
 * in SoftphoneProvider.
 */
import * as React from "react";
import { Grid3x3, Mic, MicOff, Phone, PhoneIncoming, PhoneOff, X } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { useNumbers } from "@/api/hooks";
import { useSoftphone } from "@/softphone/SoftphoneProvider";
import { Button, Input } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

const KEYPAD_ROWS = [
  ["1", "2", "3"],
  ["4", "5", "6"],
  ["7", "8", "9"],
  ["*", "0", "#"],
];

function callerIdStorageKey(orgId: string | null): string {
  return `csaas.softphone.callerId.${orgId ?? "none"}`;
}

function statusLabel(status: string): string {
  switch (status) {
    case "connecting":
      return "Connecting…";
    case "ringing-out":
      return "Ringing…";
    case "reconnecting":
      return "Reconnecting…";
    default:
      return status;
  }
}

function formatElapsed(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** A subtle repeating two-tone ring, no audio asset. Silently no-ops where AudioContext
 * isn't available (jsdom in tests, locked-down browsers). */
function useRingTone(active: boolean) {
  React.useEffect(() => {
    if (!active) return undefined;
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return undefined;

    let ctx: AudioContext;
    try {
      ctx = new Ctx();
    } catch {
      return undefined;
    }

    let stopped = false;
    const beep = () => {
      if (stopped) return;
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = 440;
      gain.gain.value = 0.05;
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.3);
    };
    beep();
    const interval = setInterval(beep, 2000);

    return () => {
      stopped = true;
      clearInterval(interval);
      ctx.close().catch(() => undefined);
    };
  }, [active]);
}

export function SoftphonePanel() {
  const { api, orgId } = useAuth();
  const { data: numbers } = useNumbers(api);
  const softphone = useSoftphone();
  const [expanded, setExpanded] = React.useState(false);
  const [to, setTo] = React.useState("");
  const [from, setFrom] = React.useState("");
  const [dialError, setDialError] = React.useState<string | null>(null);
  const [showKeypad, setShowKeypad] = React.useState(false);
  const [dtmfInput, setDtmfInput] = React.useState("");
  const [elapsed, setElapsed] = React.useState(0);

  const activeNumbers = React.useMemo(() => (numbers ?? []).filter((n) => n.is_active), [numbers]);

  useRingTone(softphone.incoming.length > 0);

  // Restore the per-org caller-ID choice, and keep it in sync as the org switches.
  React.useEffect(() => {
    try {
      const stored = localStorage.getItem(callerIdStorageKey(orgId));
      setFrom(stored && activeNumbers.some((n) => n.e164 === stored) ? stored : "");
    } catch {
      setFrom("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [orgId]);

  React.useEffect(() => {
    if (!from) return;
    try {
      localStorage.setItem(callerIdStorageKey(orgId), from);
    } catch {
      /* private mode - the choice simply won't persist */
    }
  }, [from, orgId]);

  React.useEffect(() => {
    if (softphone.status !== "in-call") {
      setElapsed(0);
      return undefined;
    }
    const start = Date.now();
    const interval = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(interval);
  }, [softphone.status]);

  React.useEffect(() => {
    if (softphone.status !== "idle") setShowKeypad(false);
  }, [softphone.status]);

  React.useEffect(() => {
    if (softphone.activeCall || softphone.incoming.length > 0) setExpanded(true);
  }, [softphone.activeCall, softphone.incoming.length]);

  async function dial(e: React.FormEvent) {
    e.preventDefault();
    setDialError(null);
    try {
      await softphone.dial(to, from || undefined);
      setTo("");
    } catch (err) {
      setDialError((err as Error).message);
    }
  }

  async function pressDigit(digit: string) {
    setDtmfInput((prev) => prev + digit);
    try {
      await softphone.sendDtmf(digit);
    } catch {
      /* keypad stays responsive even if a tone fails to send */
    }
  }

  const busy = softphone.status !== "idle";
  const showBadge = busy && softphone.status !== "in-call";

  if (!expanded && softphone.incoming.length === 0 && !softphone.activeCall) {
    return (
      <div className="fixed bottom-4 right-4 z-50">
        <Button
          type="button"
          size="icon"
          className="h-12 w-12 rounded-full shadow-lg"
          aria-label="Open softphone"
          onClick={() => setExpanded(true)}
        >
          <Phone className="h-5 w-5" />
        </Button>
      </div>
    );
  }

  return (
    <div
      className="fixed bottom-4 right-4 z-50 w-80 rounded-lg border border-border bg-background shadow-xl"
      aria-label="Softphone"
    >
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="text-sm font-medium">Softphone</span>
        {!softphone.activeCall && softphone.incoming.length === 0 && (
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="h-7 w-7"
            aria-label="Collapse softphone"
            onClick={() => setExpanded(false)}
          >
            <X className="h-4 w-4" />
          </Button>
        )}
      </div>

      {softphone.incoming.map((ring) => (
        <div key={ring.callId} className="space-y-2 border-b border-border p-3" role="alert">
          <div className="flex items-center gap-2 text-sm font-medium">
            <PhoneIncoming className="h-4 w-4 text-green-600" />
            Incoming call — {formatPhone(ring.from)}
          </div>
          <p className="text-xs text-muted-foreground">to {formatPhone(ring.to)}</p>
          <div className="flex gap-2">
            <Button
              type="button"
              className="flex-1"
              onClick={() => softphone.answer(ring.callId)}
            >
              Answer
            </Button>
            <Button
              type="button"
              variant="destructive"
              className="flex-1"
              onClick={() => softphone.decline(ring.callId)}
            >
              Decline
            </Button>
          </div>
        </div>
      ))}

      {softphone.activeCall ? (
        <div className="space-y-3 p-3">
          {showBadge && (
            <span
              role="status"
              className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-800"
            >
              {statusLabel(softphone.status)}
            </span>
          )}
          {softphone.deviceError && (
            <p role="alert" className="text-xs text-destructive">
              {softphone.deviceError}
            </p>
          )}
          <div>
            <p className="text-sm font-medium">{formatPhone(softphone.activeCall.contact)}</p>
            <p className="text-xs text-muted-foreground">{formatElapsed(elapsed)}</p>
          </div>

          {showKeypad && (
            <div className="space-y-2">
              <Input readOnly aria-label="DTMF digits sent" value={dtmfInput} />
              <div className="grid grid-cols-3 gap-1">
                {KEYPAD_ROWS.flat().map((digit) => (
                  <Button
                    key={digit}
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={!softphone.dtmfSupported}
                    title={
                      softphone.dtmfSupported
                        ? undefined
                        : "DTMF requires a newer livekit-client"
                    }
                    onClick={() => pressDigit(digit)}
                  >
                    {digit}
                  </Button>
                ))}
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-2">
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Microphone
              <select
                aria-label="Microphone"
                className="h-8 rounded-md border border-border bg-background px-1 text-xs"
                value={softphone.selectedInputId ?? ""}
                onChange={(e) =>
                  softphone.setAudioDevices(e.target.value || null, softphone.selectedOutputId)
                }
              >
                <option value="">Default</option>
                {softphone.devices.inputs.map((d) => (
                  <option key={d.deviceId} value={d.deviceId}>
                    {d.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted-foreground">
              Speaker
              <select
                aria-label="Speaker"
                className="h-8 rounded-md border border-border bg-background px-1 text-xs"
                value={softphone.selectedOutputId ?? ""}
                onChange={(e) =>
                  softphone.setAudioDevices(softphone.selectedInputId, e.target.value || null)
                }
              >
                <option value="">Default</option>
                {softphone.devices.outputs.map((d) => (
                  <option key={d.deviceId} value={d.deviceId}>
                    {d.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="flex items-center gap-2">
            <Button
              type="button"
              size="icon"
              variant="outline"
              aria-label={softphone.muted ? "Unmute" : "Mute"}
              aria-pressed={softphone.muted}
              onClick={() => softphone.setMuted(!softphone.muted)}
            >
              {softphone.muted ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
            </Button>
            <Button
              type="button"
              size="icon"
              variant="outline"
              aria-label="Toggle keypad"
              aria-pressed={showKeypad}
              onClick={() => setShowKeypad((v) => !v)}
            >
              <Grid3x3 className="h-4 w-4" />
            </Button>
            <Button
              type="button"
              variant="destructive"
              className="flex-1"
              onClick={() => softphone.hangUp()}
            >
              <PhoneOff className="mr-1 h-4 w-4" /> Hang up
            </Button>
          </div>
        </div>
      ) : softphone.incoming.length === 0 ? (
        <form className="space-y-2 p-3" onSubmit={dial}>
          {showBadge && (
            <span
              role="status"
              className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-800"
            >
              {statusLabel(softphone.status)}
            </span>
          )}
          <Input
            aria-label="Number to call"
            placeholder="+19725550199"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            disabled={busy}
          />
          <select
            aria-label="Call from"
            className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            disabled={busy}
          >
            <option value="">Any active number</option>
            {activeNumbers.map((n) => (
              <option key={n.id} value={n.e164}>
                {formatPhone(n.e164)}
              </option>
            ))}
          </select>
          <Button type="submit" className="w-full" disabled={busy || !to.trim()}>
            <Phone className="mr-1 h-4 w-4" /> Call
          </Button>
          {dialError && (
            <p role="alert" className={cn("text-sm text-destructive")}>
              {dialError}
            </p>
          )}
        </form>
      ) : null}
    </div>
  );
}
