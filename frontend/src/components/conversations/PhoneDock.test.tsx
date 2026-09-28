import * as React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PhoneDock } from "./PhoneDock";

const mockValue = vi.hoisted(() => ({
  activeCall: null as { id: string; room: string; contact: string } | null,
  muted: false,
  onHold: false,
  setMuted: vi.fn(async (_muted: boolean) => {}),
  setHold: vi.fn(async (_on: boolean) => {}),
  hangUp: vi.fn(async () => {}),
}));

vi.mock("@/softphone/SoftphoneProvider", () => ({
  useSoftphone: () => mockValue,
}));

const FROM_OPTIONS = [
  { e164: "+15550001111", label: "Main line" },
  { e164: "+15550002222", label: "Sales" },
];

const IN_CALL = { id: "call-1", room: "room-1", contact: "Ada Lovelace" };

function silentCall() {
  return vi.fn(async (_vars: { to: string; from: string }) => {});
}

function renderDock(overrides: Partial<React.ComponentProps<typeof PhoneDock>> = {}) {
  return render(<PhoneDock fromOptions={FROM_OPTIONS} onCall={silentCall()} {...overrides} />);
}

beforeEach(() => {
  mockValue.activeCall = null;
  mockValue.muted = false;
  mockValue.onHold = false;
  vi.clearAllMocks();
});

describe("PhoneDock", () => {
  it("renders the dialer when idle", () => {
    renderDock();

    expect(screen.getByLabelText("Phone")).toBeTruthy();
    expect(screen.getByLabelText("Number to call")).toBeTruthy();
    expect(screen.getByLabelText("Call from")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Call" })).toBeTruthy();
  });

  it("keeps Call disabled until a number is typed", async () => {
    const user = userEvent.setup();
    renderDock();

    const call = screen.getByRole("button", { name: "Call" }) as HTMLButtonElement;
    expect(call.disabled).toBe(true);

    await user.type(screen.getByLabelText("Number to call"), "+15551234567");

    expect(call.disabled).toBe(false);
  });

  it("places a call with the trimmed number and the chosen line", async () => {
    const user = userEvent.setup();
    const onCall = silentCall();
    renderDock({ onCall });

    const input = screen.getByLabelText("Number to call") as HTMLInputElement;
    await user.type(input, "  +15551234567  ");
    await user.selectOptions(screen.getByLabelText("Call from"), "+15550002222");
    await user.click(screen.getByRole("button", { name: "Call" }));

    expect(onCall).toHaveBeenCalledWith({ to: "+15551234567", from: "+15550002222" });
    await waitFor(() => expect(input.value).toBe(""));
  });

  it("places the call when Enter is pressed in the number field", async () => {
    const user = userEvent.setup();
    const onCall = silentCall();
    renderDock({ onCall });

    await user.type(screen.getByLabelText("Number to call"), "+15551234567{Enter}");

    expect(onCall).toHaveBeenCalledWith({ to: "+15551234567", from: "+15550001111" });
  });

  it("shows the only line as text instead of a select", () => {
    renderDock({ fromOptions: [{ e164: "+15550001111", label: "Main line" }] });

    expect(screen.queryByLabelText("Call from")).toBeNull();
    expect(screen.getByText("From Main line")).toBeTruthy();
  });

  it("shows a failed call in the alert region and keeps the number", async () => {
    const user = userEvent.setup();
    const onCall = vi.fn(async (_vars: { to: string; from: string }) => {
      throw new Error("Line is busy");
    });
    renderDock({ onCall });

    const input = screen.getByLabelText("Number to call") as HTMLInputElement;
    await user.type(input, "+15551234567");
    await user.click(screen.getByRole("button", { name: "Call" }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("Line is busy"));
    expect(input.value).toBe("+15551234567");
  });

  it("renders nothing without lines and without a call", () => {
    const { container } = renderDock({ fromOptions: [] });

    expect(container.childNodes).toHaveLength(0);
  });

  it("shows the contact and counts the call time up", () => {
    vi.useFakeTimers();
    try {
      mockValue.activeCall = IN_CALL;
      renderDock();

      expect(screen.getByText("Ada Lovelace")).toBeTruthy();
      expect(screen.getByLabelText("Call time").textContent).toBe("00:00");

      act(() => {
        vi.advanceTimersByTime(3000);
      });
      expect(screen.getByLabelText("Call time").textContent).toBe("00:03");

      act(() => {
        vi.advanceTimersByTime(57_000);
      });
      expect(screen.getByLabelText("Call time").textContent).toBe("01:00");
    } finally {
      vi.useRealTimers();
    }
  });

  it("wires Mute, Hold and End to the softphone", async () => {
    const user = userEvent.setup();
    mockValue.activeCall = IN_CALL;
    renderDock();

    const mute = screen.getByRole("button", { name: "Mute" });
    expect(mute.getAttribute("aria-pressed")).toBe("false");
    await user.click(mute);
    expect(mockValue.setMuted).toHaveBeenCalledWith(true);

    const hold = screen.getByRole("button", { name: "Hold" });
    expect(hold.getAttribute("aria-pressed")).toBe("false");
    await user.click(hold);
    expect(mockValue.setHold).toHaveBeenCalledWith(true);

    await user.click(screen.getByRole("button", { name: "End" }));
    expect(mockValue.hangUp).toHaveBeenCalledTimes(1);
  });

  it("shows Unmute and Resume for a muted, held call", async () => {
    const user = userEvent.setup();
    mockValue.activeCall = IN_CALL;
    mockValue.muted = true;
    mockValue.onHold = true;
    renderDock();

    const unmute = screen.getByRole("button", { name: "Unmute" });
    expect(unmute.getAttribute("aria-pressed")).toBe("true");
    await user.click(unmute);
    expect(mockValue.setMuted).toHaveBeenCalledWith(false);

    const resume = screen.getByRole("button", { name: "Resume" });
    expect(resume.getAttribute("aria-pressed")).toBe("true");
    await user.click(resume);
    expect(mockValue.setHold).toHaveBeenCalledWith(false);
  });

  it("reports a failed call control in the alert region", async () => {
    const user = userEvent.setup();
    mockValue.activeCall = IN_CALL;
    mockValue.hangUp.mockImplementationOnce(async () => {
      throw new Error("Network down");
    });
    renderDock();

    await user.click(screen.getByRole("button", { name: "End" }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("Network down"));
  });
});
