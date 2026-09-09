/**
 * The app-wide softphone dock (plan phase-6-plan.md deliverable 4). Fixed bottom-right,
 * visible on every authed page. Pure UI over useSoftphone() - all LiveKit/WS state lives
 * in SoftphoneProvider.
 */
import * as React from "react";
import {
  Bell,
  BellOff,
  Grid3x3,
  Mic,
  MicOff,
  Phone,
  PhoneIncoming,
  PhoneOff,
  X,
} from "lucide-react";
import { hasPermission, useAuth } from "@/auth/AuthContext";
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

const RINGTONE_MUTE_KEY = "csaas.softphone.ringtoneMuted";

/** A subtle repeating two-tone ring, no audio asset. Silently no-ops where AudioContext
 * isn't available (jsdom in tests, locked-down browsers).
 *
 * Item 30: an AudioContext created without a prior user gesture starts life
 * "suspended" under browser autoplay policy - an inbound ring is not itself a user
 * gesture, so a freshly-opened tab's first ring can be dead silent even though this
 * hook runs. A one-time capture-phase listener resumes the (possibly still-suspended)
 * context the moment the operator interacts with the page at all. Also honors a
 * per-browser mute toggle so a muted ringer stays muted across calls until unmuted. */
function useRingTone(active: boolean, muted: boolean) {
  const ctxRef = React.useRef<AudioContext | null>(null);

  React.useEffect(() => {
    const Ctx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return undefined;

    function resume() {
      ctxRef.current?.resume().catch(() => undefined);
    }
    document.addEventListener("pointerdown", resume, { capture: true });
    document.addEventListener("keydown", resume, { capture: true });
    return () => {
      document.removeEventListener("pointerdown", resume, { capture: true });
      document.removeEventListener("keydown", resume, { capture: true });
    };
  }, []);

  React.useEffect(() => {
    if (!active || muted) return undefined;
    const Ctx =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctx) return undefined;

    let ctx: AudioContext;
    try {
      ctx = new Ctx();
      ctxRef.current = ctx;
    } catch {
      return undefined;
    }
    // In case the context was created already-resumed-eligible (a gesture happened
    // before this ring started), try immediately too - the listeners above cover the
    // "ring started before any gesture" case.
    ctx.resume().catch(() => undefined);

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
      if (ctxRef.current === ctx) ctxRef.current = null;
    };
  }, [active, muted]);
}

export function SoftphonePanel() {
  const { api, me, orgId } = useAuth();
  const { data: numbers } = useNumbers(api);
  const softphone = useSoftphone();
  const [expanded, setExpanded] = React.useState(false);
  const [to, setTo] = React.useState("");
  const [from, setFrom] = React.useState("");
  const [dialError, setDialError] = React.useState<string | null>(null);
  const [answerError, setAnswerError] = React.useState<string | null>(null);
  const [declineError, setDeclineError] = React.useState<string | null>(null);
  const [hangupError, setHangupError] = React.useState<string | null>(null);
  const [showKeypad, setShowKeypad] = React.useState(false);
  const [dtmfInput, setDtmfInput] = React.useState("");
  const [elapsed, setElapsed] = React.useState(0);
  const [ringtoneMuted, setRingtoneMuted] = React.useState(() => {
    try {
      return localStorage.getItem(RINGTONE_MUTE_KEY) === "true";
    } catch {
      return false;
    }
  });
  // Item 32: tracks which incoming ring ids currently have an answer/decline request in
  // flight, so a double-click (or a slow network) can't fire the same action twice.
  const [pendingRingIds, setPendingRingIds] = React.useState<Set<string>>(new Set());
  const answerButtonRef = React.useRef<HTMLButtonElement>(null);

  const activeNumbers = React.useMemo(() => (numbers ?? []).filter((n) => n.is_active), [numbers]);
  // Item 5: undefined `permissions` (backend hasn't rolled them out for this membership
  // yet) fails OPEN - only an explicit, present, and missing "calls:place" disables this.
  const canPlaceCalls = hasPermission(me, orgId, "calls:place");

  useRingTone(softphone.incoming.length > 0, ringtoneMuted);

  function toggleRingtoneMuted() {
    setRingtoneMuted((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(RINGTONE_MUTE_KEY, String(next));
      } catch {
        /* private mode - the choice simply won't persist */
      }
      return next;
    });
  }

  // Item 15: the stored caller-ID choice is only trustworthy once the numbers list has
  // actually loaded - deciding while `numbers` is still undefined (fetch in flight, e.g.
  // right after the queryClient.clear() on login/org-switch) always sees an empty
  // `activeNumbers` and wrongly clears a perfectly valid stored choice.
  React.useEffect(() => {
    if (numbers === undefined) return;
    try {
      const stored = localStorage.getItem(callerIdStorageKey(orgId));
      setFrom(stored && activeNumbers.some((n) => n.e164 === stored) ? stored : "");
    } catch {
      setFrom("");
    }
  }, [orgId, numbers, activeNumbers]);

  // Item 10: this used to bail out on an empty `from`, so choosing "Any active number"
  // (the empty-string option) was never actually persisted - the read effect above would
  // just fall back to whatever was stored from before. Gate on `numbers` having loaded
  // instead (mirroring the read effect) so this only starts persisting - including an
  // intentional empty choice - once the read effect has had its own chance to run first;
  // that keeps this from clobbering a real stored number with "" before it's ever loaded.
  React.useEffect(() => {
    if (numbers === undefined) return;
    try {
      localStorage.setItem(callerIdStorageKey(orgId), from);
    } catch {
      /* private mode - the choice simply won't persist */
    }
  }, [from, orgId, numbers]);

  // Item 28 companion: this timer is keyed on the CALL's identity, not merely on
  // status !== "in-call" - a mid-call ICE hiccup flips status to "reconnecting" and
  // back without this call ever ending, and must not restart the clock from 0:00.
  const activeCallId = softphone.activeCall?.id ?? null;
  const callStartRef = React.useRef<number | null>(null);
  React.useEffect(() => {
    if (!activeCallId) {
      callStartRef.current = null;
      setElapsed(0);
      return undefined;
    }
    if (softphone.status !== "in-call" && softphone.status !== "reconnecting") return undefined;
    if (callStartRef.current === null) callStartRef.current = Date.now();
    const start = callStartRef.current;
    const interval = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => clearInterval(interval);
  }, [activeCallId, softphone.status]);

  React.useEffect(() => {
    if (softphone.status !== "idle") setShowKeypad(false);
  }, [softphone.status]);

  // Item 29: DTMF digits sent on one call must never bleed into the next.
  React.useEffect(() => {
    setDtmfInput("");
  }, [activeCallId]);

  React.useEffect(() => {
    if (softphone.activeCall || softphone.incoming.length > 0) setExpanded(true);
  }, [softphone.activeCall, softphone.incoming.length]);

  // Item 9: a stale answerError (from a ring that failed to answer and was then
  // withdrawn/claimed elsewhere) must not keep showing once there's no incoming ring left
  // for it to be about.
  React.useEffect(() => {
    if (softphone.incoming.length === 0) setAnswerError(null);
  }, [softphone.incoming.length]);

  // Item 53: move focus onto the (first) Answer button the moment a ring arrives, and
  // offer Alt+A as a hands-free way to answer it without reaching for the mouse.
  const firstIncomingId = softphone.incoming[0]?.callId ?? null;
  React.useEffect(() => {
    if (!firstIncomingId) return;
    answerButtonRef.current?.focus();
  }, [firstIncomingId]);

  React.useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (!e.altKey || e.key.toLowerCase() !== "a") return;
      const ring = softphone.incoming[0];
      if (!ring || pendingRingIds.has(ring.callId) || !canPlaceCalls) return;
      e.preventDefault();
      void handleAnswer(ring.callId);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [softphone.incoming, pendingRingIds, canPlaceCalls]);

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

  async function handleAnswer(callId: string) {
    setAnswerError(null);
    setPendingRingIds((prev) => new Set(prev).add(callId));
    try {
      await softphone.answer(callId);
    } catch (err) {
      setAnswerError((err as Error).message);
    } finally {
      setPendingRingIds((prev) => {
        const next = new Set(prev);
        next.delete(callId);
        return next;
      });
    }
  }

  // Item 3: mirrors handleAnswer - a failed decline is caught and surfaced instead of
  // becoming an unhandled rejection (the provider now also keeps the ring card up until
  // the hangup POST actually resolves, so there's something visible left to attach the
  // error to).
  async function handleDecline(callId: string) {
    setDeclineError(null);
    setPendingRingIds((prev) => new Set(prev).add(callId));
    try {
      await softphone.decline(callId);
    } catch (err) {
      setDeclineError((err as Error).message);
    } finally {
      setPendingRingIds((prev) => {
        const next = new Set(prev);
        next.delete(callId);
        return next;
      });
    }
  }

  async function handleHangUp() {
    setHangupError(null);
    try {
      await softphone.hangUp();
    } catch (err) {
      setHangupError((err as Error).message);
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
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="h-7 w-7"
          aria-label={ringtoneMuted ? "Unmute ringtone" : "Mute ringtone"}
          aria-pressed={ringtoneMuted}
          onClick={toggleRingtoneMuted}
        >
          {ringtoneMuted ? <BellOff className="h-4 w-4" /> : <Bell className="h-4 w-4" />}
        </Button>
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

      {answerError && (
        <p role="alert" className="border-b border-border px-3 py-2 text-xs text-destructive">
          {answerError}
        </p>
      )}

      {declineError && (
        <p role="alert" className="border-b border-border px-3 py-2 text-xs text-destructive">
          {declineError}
        </p>
      )}

      {/* Item 2: rendered here (outside the `softphone.activeCall ?` branch below) so a
          hangup failure stays visible even once the Disconnected handler has already
          nulled activeCall out from under it (e.g. the API leg failed but the LiveKit
          room disconnect succeeded). */}
      {hangupError && (
        <p role="alert" className="border-b border-border px-3 py-2 text-xs text-destructive">
          {hangupError}
        </p>
      )}

      {softphone.incoming.map((ring, index) =>
        ring.kind === "handoff" ? (
          <div
            key={ring.callId}
            className="space-y-2 border-b border-border bg-amber-50 p-3"
            role="alert"
            aria-label="AI handoff"
          >
            <div className="flex items-center gap-2 text-sm font-semibold text-amber-900">
              <PhoneIncoming className="h-4 w-4 text-amber-600" />
              AI handoff — {formatPhone(ring.from)}
            </div>
            {ring.reason && (
              <p className="text-xs font-medium text-amber-800">Reason: {ring.reason}</p>
            )}
            {ring.summary && <p className="text-xs text-amber-800">{ring.summary}</p>}
            <Button
              ref={index === 0 ? answerButtonRef : undefined}
              type="button"
              className="w-full"
              disabled={pendingRingIds.has(ring.callId) || !canPlaceCalls}
              title={canPlaceCalls ? undefined : "You don't have permission to place or answer calls"}
              onClick={() => handleAnswer(ring.callId)}
            >
              {pendingRingIds.has(ring.callId) ? "Joining…" : "Join call"}
            </Button>
          </div>
        ) : (
          <div key={ring.callId} className="space-y-2 border-b border-border p-3" role="alert">
            <div className="flex items-center gap-2 text-sm font-medium">
              <PhoneIncoming className="h-4 w-4 text-green-600" />
              Incoming call — {formatPhone(ring.from)}
            </div>
            <p className="text-xs text-muted-foreground">to {formatPhone(ring.to)}</p>
            <div className="flex gap-2">
              <Button
                ref={index === 0 ? answerButtonRef : undefined}
                type="button"
                className="flex-1"
                disabled={pendingRingIds.has(ring.callId) || !canPlaceCalls}
                title={canPlaceCalls ? undefined : "You don't have permission to place or answer calls"}
                onClick={() => handleAnswer(ring.callId)}
              >
                {pendingRingIds.has(ring.callId) ? "Answering…" : "Answer"}
              </Button>
              <Button
                type="button"
                variant="destructive"
                className="flex-1"
                disabled={pendingRingIds.has(ring.callId)}
                onClick={() => handleDecline(ring.callId)}
              >
                Decline
              </Button>
            </div>
          </div>
        ),
      )}

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
              onClick={() => softphone.setMuted(!softphone.muted).catch(() => undefined)}
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
            <Button type="button" variant="destructive" className="flex-1" onClick={handleHangUp}>
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
          <Button
            type="submit"
            className="w-full"
            disabled={busy || !to.trim() || !canPlaceCalls}
            title={canPlaceCalls ? undefined : "You don't have permission to place or answer calls"}
          >
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
