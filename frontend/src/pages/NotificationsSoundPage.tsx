import * as React from "react";

import { Button, Card } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { useSoftphone } from "@/softphone/SoftphoneProvider";

/*
 * "Notifications & sound": desktop alert permission, the softphone's ringtone toggle, and
 * the microphone/speaker pickers the phone panel uses. Every setting on this page is a
 * browser- or softphone-level preference, so nothing here talks to the API itself.
 */

/** The softphone already reads and writes this exact key when it decides whether to ring,
 * so the choice made here is the same choice the phone panel honours. */
const RINGTONE_MUTED_KEY = "csaas.softphone.ringtoneMuted";
/** Same-tab notification for an already-mounted softphone (storage events only reach other
 * tabs). Must match RINGTONE_MUTED_EVENT in softphone/SoftphonePanel.tsx. */
const RINGTONE_MUTED_EVENT = "csaas:ringtone-muted";

type DesktopAlertState = "granted" | "default" | "denied" | "unsupported";

/** Only the parts of the Notification API this page uses - `window.Notification` is
 * missing entirely in some embedded browsers (and under jsdom until a test stubs it). */
type NotificationApi = {
  permission?: string;
  requestPermission?: () => Promise<unknown>;
};

function getNotificationApi(): NotificationApi | undefined {
  if (typeof window === "undefined") return undefined;
  return window.Notification as NotificationApi | undefined;
}

function readDesktopAlertState(): DesktopAlertState {
  const permission = getNotificationApi()?.permission;
  if (permission === "granted" || permission === "denied" || permission === "default") {
    return permission;
  }
  // No Notification API, or a permission value we don't recognise: either way this
  // browser is not going to show desktop alerts.
  return "unsupported";
}

const DESKTOP_ALERT_COPY: Record<DesktopAlertState, string> = {
  granted: "Desktop alerts are on. You'll get one for new texts, missed calls and mentions.",
  default: "Get an alert on your desktop for new texts, missed calls and mentions.",
  denied:
    "Your browser is blocking alerts for Ringlite. Allow notifications for this site in your browser's site settings, then reload.",
  unsupported: "This browser can't show desktop alerts.",
};

function readRingtoneMuted(): boolean {
  try {
    return window.localStorage.getItem(RINGTONE_MUTED_KEY) === "true";
  } catch {
    // Storage can be blocked (private mode, third-party cookie rules). The ringtone then
    // stays on, which is the safe default for a phone.
    return false;
  }
}

function writeRingtoneMuted(muted: boolean): void {
  try {
    window.localStorage.setItem(RINGTONE_MUTED_KEY, muted ? "true" : "false");
  } catch {
    // Nothing to persist to; the toggle still applies for this tab.
  }
}

/** A short two-tone chime through the Web Audio API: 440 Hz, then 480 Hz, ending at 0.6s
 * with the gain held at 0.15. The graph is built fresh per click and closed once it has
 * played, so there is nothing long-lived to clean up. */
function playTestTone(): void {
  const context = new window.AudioContext();
  try {
    const gain = context.createGain();
    gain.gain.setValueAtTime(0.15, context.currentTime);
    gain.connect(context.destination);

    const oscillator = context.createOscillator();
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(440, context.currentTime);
    oscillator.frequency.setValueAtTime(480, context.currentTime + 0.3);
    oscillator.connect(gain);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.6);

    window.setTimeout(() => {
      void context.close();
    }, 600);
  } catch (err) {
    try {
      void context.close();
    } catch {
      // The context never got far enough to be closable.
    }
    throw err;
  }
}

export function NotificationsSoundPage(): JSX.Element {
  const {
    devices,
    selectedInputId,
    selectedOutputId,
    deviceError,
    setAudioDevices,
    refreshDevices,
  } = useSoftphone();

  const [desktopAlerts, setDesktopAlerts] =
    React.useState<DesktopAlertState>(readDesktopAlertState);
  const [requestingAlerts, setRequestingAlerts] = React.useState(false);

  const [ringtoneMuted, setRingtoneMuted] = React.useState<boolean>(readRingtoneMuted);
  const [soundError, setSoundError] = React.useState<string | null>(null);

  const audioSupported =
    typeof window !== "undefined" && typeof window.AudioContext !== "undefined";

  const requestDesktopAlerts = async (): Promise<void> => {
    const api = getNotificationApi();
    if (!api || typeof api.requestPermission !== "function") return;
    setRequestingAlerts(true);
    try {
      await api.requestPermission();
    } catch {
      // Some browsers reject instead of resolving; either way the real permission is
      // re-read below rather than assumed.
    } finally {
      setDesktopAlerts(readDesktopAlertState());
      setRequestingAlerts(false);
    }
  };

  const toggleRingtone = (event: React.ChangeEvent<HTMLInputElement>): void => {
    const muted = !event.target.checked;
    setRingtoneMuted(muted);
    writeRingtoneMuted(muted);
    try {
      window.dispatchEvent(new CustomEvent(RINGTONE_MUTED_EVENT, { detail: { muted } }));
    } catch {
      // CustomEvent unavailable: other tabs still pick it up through the storage event.
    }
  };

  const playSound = (): void => {
    setSoundError(null);
    try {
      playTestTone();
    } catch {
      setSoundError("Couldn't play a sound in this browser.");
    }
  };

  const noDevices = devices.inputs.length === 0 && devices.outputs.length === 0;

  const selectClassName = cn(
    "w-full rounded-md border border-slate-300 bg-white px-2 py-1 text-sm",
    "focus:border-slate-400 focus:outline-none",
  );

  return (
    <div className="space-y-6">
      <section aria-labelledby="notifications-desktop-alerts-heading">
        <Card className="space-y-3 p-4">
          <h2 id="notifications-desktop-alerts-heading" className="text-base font-semibold">
            Desktop alerts
          </h2>
          <p className="text-sm text-slate-600">{DESKTOP_ALERT_COPY[desktopAlerts]}</p>
          {desktopAlerts === "default" ? (
            <Button
              type="button"
              onClick={() => void requestDesktopAlerts()}
              disabled={requestingAlerts}
            >
              Turn on desktop alerts
            </Button>
          ) : null}
        </Card>
      </section>

      <section aria-labelledby="notifications-ringtone-heading">
        <Card className="space-y-3 p-4">
          <h2 id="notifications-ringtone-heading" className="text-base font-semibold">
            Ringtone
          </h2>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              aria-label="Play a ringtone for incoming calls"
              checked={!ringtoneMuted}
              onChange={toggleRingtone}
            />
            <span>Play a ringtone for incoming calls</span>
          </label>
          <p className="text-xs text-slate-500">Applies the next time the phone panel opens.</p>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" onClick={playSound} disabled={!audioSupported}>
              Play a test sound
            </Button>
          </div>
          {soundError ? (
            <p role="alert" className="text-sm text-red-600">
              {soundError}
            </p>
          ) : null}
        </Card>
      </section>

      <section aria-labelledby="notifications-audio-devices-heading">
        <Card className="space-y-3 p-4">
          <h2 id="notifications-audio-devices-heading" className="text-base font-semibold">
            Microphone and speaker
          </h2>
          {noDevices ? (
            <p className="text-sm text-slate-500">
              Allow microphone access in the phone panel to see your devices here.
            </p>
          ) : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <span className="block text-xs font-medium text-slate-500">Microphone</span>
              <select
                aria-label="Microphone"
                className={selectClassName}
                value={selectedInputId ?? ""}
                onChange={(event) => {
                  void setAudioDevices(event.target.value || null, selectedOutputId);
                }}
              >
                <option value="">System default</option>
                {devices.inputs.map((device) => (
                  <option key={device.deviceId} value={device.deviceId}>
                    {device.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1">
              <span className="block text-xs font-medium text-slate-500">Speaker</span>
              <select
                aria-label="Speaker"
                className={selectClassName}
                value={selectedOutputId ?? ""}
                onChange={(event) => {
                  void setAudioDevices(selectedInputId, event.target.value || null);
                }}
              >
                <option value="">System default</option>
                {devices.outputs.map((device) => (
                  <option key={device.deviceId} value={device.deviceId}>
                    {device.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="ghost" onClick={() => void refreshDevices()}>
              Refresh devices
            </Button>
          </div>
          {deviceError ? (
            <p role="alert" className="text-sm text-red-600">
              {deviceError}
            </p>
          ) : null}
        </Card>
      </section>
    </div>
  );
}
