import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RecoveryCodesPage } from "./RecoveryCodesPage";

const CODES = [
  "4F7K-9QW2",
  "M2XZ-8LPA",
  "T6BC-3RVN",
  "Q9HD-5JWE",
  "Z1PU-7YSK",
  "B8GN-2MTC",
  "L3VA-6XRF",
  "W5ED-1KQH",
];

const auth = vi.hoisted(() => {
  const request = vi.fn();
  const refreshMe = vi.fn();
  const logout = vi.fn();
  const me = {
    id: "user-1",
    email: "owner@example.com",
    has_passkey: true,
    totp_enabled: false,
  };
  return {
    request,
    refreshMe,
    logout,
    value: { api: { request }, me, refreshMe, logout },
  };
});

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => auth.value,
}));

beforeEach(() => {
  auth.request.mockReset();
  auth.refreshMe.mockReset();
  auth.logout.mockReset();
  auth.request.mockResolvedValue({ codes: CODES });
  // refreshMe re-reads the session through the same client the page holds.
  auth.refreshMe.mockImplementation(async () => {
    await auth.request("/api/v1/auth/me");
  });
});

describe("RecoveryCodesPage", () => {
  it("asks for the codes once on mount and lists every code", async () => {
    render(<RecoveryCodesPage />);

    await screen.findByRole("list", { name: "Recovery codes" });

    expect(auth.request).toHaveBeenCalledTimes(1);
    expect(auth.request).toHaveBeenCalledWith("/api/v1/auth/recovery-codes", { method: "POST" });
    for (const code of CODES) {
      expect(screen.getByText(code)).toBeInTheDocument();
    }
  });

  it("generates the codes only once when React mounts the page twice", async () => {
    render(
      <React.StrictMode>
        <RecoveryCodesPage />
      </React.StrictMode>,
    );

    await screen.findByRole("list", { name: "Recovery codes" });

    expect(auth.request).toHaveBeenCalledTimes(1);
  });

  it("keeps Continue disabled until the codes are saved, then refreshes the session", async () => {
    const user = userEvent.setup();
    render(<RecoveryCodesPage />);

    const cont = await screen.findByRole("button", { name: "Continue" });
    expect(cont).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: "I have saved these codes" }));
    expect(cont).toBeEnabled();

    await user.click(cont);

    await waitFor(() => expect(auth.request).toHaveBeenCalledWith("/api/v1/auth/me"));
    expect(auth.refreshMe).toHaveBeenCalledTimes(1);
  });

  it("copies the codes to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<RecoveryCodesPage />);

    await screen.findByRole("list", { name: "Recovery codes" });
    fireEvent.click(screen.getByRole("button", { name: "Copy codes" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(CODES.join("\n")));
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("shows the error and offers Try again when generating the codes fails", async () => {
    const user = userEvent.setup();
    auth.request.mockRejectedValueOnce(new Error("Recovery codes are not available"));
    render(<RecoveryCodesPage />);

    expect(await screen.findByText("Recovery codes are not available")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Try again" }));

    await screen.findByRole("list", { name: "Recovery codes" });
    expect(auth.request).toHaveBeenCalledTimes(2);
  });
});
