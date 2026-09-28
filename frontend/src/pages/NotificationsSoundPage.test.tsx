// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationsSoundPage } from "@/pages/NotificationsSoundPage";
import type { SoftphoneValue } from "@/softphone/SoftphoneProvider";

const RINGTONE_MUTED_KEY = "csaas.softphone.ringtoneMuted";

/**
 * The mocked `useSoftphone` hands this straight back, so the page never sees a real
 * provider (and never touches a real LiveKit room). Every test starts from
 * `setSoftphone()`.
 */
let mockValue: SoftphoneValue;

const mockSetAudioDevices = vi.fn(() => Promise.resolve());
const mockRefreshDevices = vi.fn(() => Promise.resolve());

vi.mock("@/softphone/SoftphoneProvider", () => ({
  useSoftphone: () => mockValue,
}));

function baseSoftphoneValue(): SoftphoneValue {
  return {
    status: "idle",
    activeCall: null,
    incoming: [],
    muted: false,
    wsConnected: true,
    dtmfSupported: true,
    devices: { inputs: [], outputs: [] },
    selectedInputId: null,
    selectedOutputId: null,
    deviceError: null,
    toasts: [],
    dismissToast: () => undefined,
    dial: () => Promise.resolve(),
    answer: () => Promise.resolve(),
    decline: () => Promise.resolve(),
    hangUp: () => Promise.resolve(),
    joinInvite: () => Promise.resolve(),
    declineInvite: () => Promise.resolve(),
    leave: () => Promise.resolve(),
    parked: [],
    park: () => Promise.resolve(),
    pickup: () => Promise.resolve(),
    sendDtmf: () => Promise.resolve(),
    setMuted: () => Promise.resolve(),
    onHold: false,
    setHold: () => Promise.resolve(),
    setAudioDevices: () => Promise.resolve(),
    refreshDevices: () => Promise.resolve(),
    subscribe: () => () => undefined,
    captions: [],
  };
}

function setSoftphone(overrides: Partial<SoftphoneValue> = {}): void {
  mockValue = {
    ...baseSoftphoneValue(),
    setAudioDevices: mockSetAudioDevices,
    refreshDevices: mockRefreshDevices,
    ...overrides,
  };
}

type DesktopPermission = "default" | "granted" | "denied";

/** jsdom has no Notification API, so each test stubs one - and the stub flips its own
 * `permission` when asked, exactly like a real browser would. */
function stubNotification(initial: DesktopPermission, next: DesktopPermission = initial) {
  const notification = {
    permission: initial as string,
    requestPermission: vi.fn(() => {
      notification.permission = next;
      return Promise.resolve(next);
    }),
  };
  vi.stubGlobal("Notification", notification);
  return notification;
}

beforeEach(() => {
  window.localStorage.clear();
  vi.clearAllMocks();
  setSoftphone();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("NotificationsSoundPage desktop alerts", () => {
  it("shows the enabled copy once permission is granted", () => {
    stubNotification("granted");
    render(<NotificationsSoundPage />);

    expect(
      screen.getByText(
        "Desktop alerts are on. You'll get one for new texts, missed calls and mentions.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Turn on desktop alerts" })).toBeNull();
  });

  it("offers the enable button while permission is still default", async () => {
    const notification = stubNotification("default", "granted");
    render(<NotificationsSoundPage />);

    expect(
      screen.getByText("Get an alert on your desktop for new texts, missed calls and mentions."),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Turn on desktop alerts" }));

    await waitFor(() => {
      expect(notification.requestPermission).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(
        screen.getByText(
          "Desktop alerts are on. You'll get one for new texts, missed calls and mentions.",
        ),
      ).toBeTruthy();
    });
    expect(screen.queryByRole("button", { name: "Turn on desktop alerts" })).toBeNull();
  });

  it("explains how to unblock alerts when permission is denied", () => {
    stubNotification("denied");
    render(<NotificationsSoundPage />);

    expect(
      screen.getByText(
        "Your browser is blocking alerts for Ringlite. Allow notifications for this site in your browser's site settings, then reload.",
      ),
    ).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Turn on desktop alerts" })).toBeNull();
  });

  it("says so when the browser has no Notification API", () => {
    vi.stubGlobal("Notification", undefined);
    render(<NotificationsSoundPage />);

    expect(screen.getByText("This browser can't show desktop alerts.")).toBeTruthy();
  });
});

describe("NotificationsSoundPage ringtone", () => {
  it("reflects the stored mute preference", () => {
    window.localStorage.setItem(RINGTONE_MUTED_KEY, "true");
    render(<NotificationsSoundPage />);

    const checkbox = screen.getByRole("checkbox", {
      name: "Play a ringtone for incoming calls",
    }) as HTMLInputElement;

    expect(checkbox.checked).toBe(false);
    expect(screen.getByText("Applies the next time the phone panel opens.")).toBeTruthy();
  });

  it("writes the mute preference to localStorage", () => {
    render(<NotificationsSoundPage />);

    const checkbox = screen.getByRole("checkbox", {
      name: "Play a ringtone for incoming calls",
    }) as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
    expect(window.localStorage.getItem(RINGTONE_MUTED_KEY)).toBeNull();

    fireEvent.click(checkbox);
    expect(checkbox.checked).toBe(false);
    expect(window.localStorage.getItem(RINGTONE_MUTED_KEY)).toBe("true");

    fireEvent.click(checkbox);
    expect(checkbox.checked).toBe(true);
    expect(window.localStorage.getItem(RINGTONE_MUTED_KEY)).toBe("false");
  });

  it("dispatches csaas:ringtone-muted with the new state when toggled", () => {
    const seen: unknown[] = [];
    const handler = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener("csaas:ringtone-muted", handler);
    try {
      render(<NotificationsSoundPage />);
      const checkbox = screen.getByRole("checkbox", { name: "Play a ringtone for incoming calls" });
      fireEvent.click(checkbox);
      fireEvent.click(checkbox);
    } finally {
      window.removeEventListener("csaas:ringtone-muted", handler);
    }
    expect(seen).toEqual([{ muted: true }, { muted: false }]);
  });

  it("plays a test tone through the Web Audio API", () => {
    const frequency = { setValueAtTime: vi.fn() };
    const oscillator = {
      type: "",
      frequency,
      connect: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
    };
    const gainParam = { setValueAtTime: vi.fn() };
    const gain = { gain: gainParam, connect: vi.fn() };
    class FakeAudioContext {
      currentTime = 0;
      destination = {};
      close = vi.fn();
      createOscillator = () => oscillator;
      createGain = () => gain;
    }
    vi.stubGlobal("AudioContext", FakeAudioContext);

    render(<NotificationsSoundPage />);
    fireEvent.click(screen.getByRole("button", { name: "Play a test sound" }));

    expect(gainParam.setValueAtTime).toHaveBeenCalledWith(0.15, 0);
    expect(frequency.setValueAtTime).toHaveBeenCalledWith(440, 0);
    expect(frequency.setValueAtTime).toHaveBeenCalledWith(480, 0.3);
    expect(oscillator.start).toHaveBeenCalledTimes(1);
    expect(oscillator.stop).toHaveBeenCalledWith(0.6);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reports a failure when the sound cannot be played", () => {
    class BrokenAudioContext {
      constructor() {
        throw new Error("no audio output");
      }
    }
    vi.stubGlobal("AudioContext", BrokenAudioContext);

    render(<NotificationsSoundPage />);
    fireEvent.click(screen.getByRole("button", { name: "Play a test sound" }));

    expect(screen.getByRole("alert").textContent).toBe("Couldn't play a sound in this browser.");
  });

  it("disables the test sound button when the browser has no Web Audio", () => {
    vi.stubGlobal("AudioContext", undefined);
    render(<NotificationsSoundPage />);

    const button = screen.getByRole("button", { name: "Play a test sound" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });
});

describe("NotificationsSoundPage audio devices", () => {
  it("shows a hint until the phone panel has microphone access", () => {
    render(<NotificationsSoundPage />);

    expect(
      screen.getByText("Allow microphone access in the phone panel to see your devices here."),
    ).toBeTruthy();
  });

  it("switches the microphone and keeps the current speaker", () => {
    setSoftphone({
      devices: {
        inputs: [
          { deviceId: "mic-1", label: "Built-in microphone" },
          { deviceId: "mic-2", label: "USB microphone" },
        ],
        outputs: [{ deviceId: "spk-1", label: "Built-in speakers" }],
      },
      selectedOutputId: "spk-1",
    });
    render(<NotificationsSoundPage />);

    fireEvent.change(screen.getByLabelText("Microphone"), { target: { value: "mic-2" } });

    expect(mockSetAudioDevices).toHaveBeenCalledWith("mic-2", "spk-1");
  });

  it("switches the speaker and keeps the current microphone", () => {
    setSoftphone({
      devices: {
        inputs: [{ deviceId: "mic-1", label: "Built-in microphone" }],
        outputs: [
          { deviceId: "spk-1", label: "Built-in speakers" },
          { deviceId: "spk-2", label: "Headset" },
        ],
      },
      selectedInputId: "mic-1",
    });
    render(<NotificationsSoundPage />);

    fireEvent.change(screen.getByLabelText("Speaker"), { target: { value: "spk-2" } });

    expect(mockSetAudioDevices).toHaveBeenCalledWith("mic-1", "spk-2");
  });

  it("falls back to the system default devices", () => {
    setSoftphone({
      devices: {
        inputs: [{ deviceId: "mic-1", label: "Built-in microphone" }],
        outputs: [{ deviceId: "spk-1", label: "Built-in speakers" }],
      },
      selectedInputId: "mic-1",
      selectedOutputId: "spk-1",
    });
    render(<NotificationsSoundPage />);

    fireEvent.change(screen.getByLabelText("Microphone"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Speaker"), { target: { value: "" } });

    expect(mockSetAudioDevices).toHaveBeenNthCalledWith(1, null, "spk-1");
    expect(mockSetAudioDevices).toHaveBeenNthCalledWith(2, "mic-1", null);
  });

  it("refreshes the device list on demand", () => {
    render(<NotificationsSoundPage />);

    fireEvent.click(screen.getByRole("button", { name: "Refresh devices" }));

    expect(mockRefreshDevices).toHaveBeenCalledTimes(1);
  });

  it("surfaces a device error", () => {
    setSoftphone({ deviceError: "Microphone permission denied" });
    render(<NotificationsSoundPage />);

    expect(screen.getByRole("alert").textContent).toBe("Microphone permission denied");
  });
});
