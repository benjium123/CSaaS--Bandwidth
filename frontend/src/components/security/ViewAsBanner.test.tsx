import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loadViewAs, saveViewAs } from "@/auth/viewAs";
import { formatRemaining, ViewAsBanner } from "@/components/security/ViewAsBanner";

const api = {
  auth: { orgId: "operator-org" as string | null },
  setAuth: vi.fn(),
  request: vi.fn(),
};

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ api, me: null }),
}));

const originalLocation = window.location;
const assign = vi.fn();

const EXPIRES_IN_MS = 90_000;

function storeView(expiresInMs = EXPIRES_IN_MS) {
  saveViewAs({
    id: "va-1",
    orgId: "org-1",
    orgName: "Acme Corp",
    expiresAt: new Date(Date.now() + expiresInMs).toISOString(),
    permissions: ["read"],
    prevOrgId: "operator-org",
  });
}

beforeEach(() => {
  sessionStorage.clear();
  api.request.mockReset();
  api.setAuth.mockReset();
  api.request.mockResolvedValue({ ended: true });
  assign.mockReset();
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: { assign },
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: originalLocation,
  });
});

const formatCases: Array<[number, string]> = [
  [90_000, "01:30"],
  [60_000, "01:00"],
  [1_000, "00:01"],
  [59_999, "00:59"],
  [0, "00:00"],
  [-5_000, "00:00"],
  [3_600_000, "60:00"],
];

describe("formatRemaining", () => {
  it.each(formatCases)("formats %i ms as %s", (ms, expected) => {
    expect(formatRemaining(ms)).toBe(expected);
  });
});

describe("ViewAsBanner", () => {
  it("renders nothing without a stored view", () => {
    render(<ViewAsBanner />);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows the workspace name and the remaining time", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-01T00:00:00.000Z"));
    storeView();

    render(<ViewAsBanner />);

    expect(screen.getByText("Acme Corp")).toBeTruthy();
    expect(screen.getByText(/01:30/)).toBeTruthy();
  });

  it("ends the view, restores the operator workspace and navigates on exit", async () => {
    const user = userEvent.setup();
    storeView();

    render(<ViewAsBanner />);
    await user.click(screen.getByRole("button", { name: "Exit view" }));
    await act(async () => {});

    expect(api.request).toHaveBeenCalledWith("/api/v1/ops/view-as/va-1/end", {
      method: "POST",
    });
    expect(api.setAuth).toHaveBeenCalledWith({ orgId: "operator-org" });
    expect(loadViewAs({ includeExpired: true })).toBeNull();
    expect(assign).toHaveBeenCalledWith("/ops");
  });

  it("ends the view automatically once the grant expires", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-01T00:00:00.000Z"));
    storeView(2_000);

    render(<ViewAsBanner />);
    expect(screen.getByText("Acme Corp")).toBeTruthy();

    await act(async () => {
      vi.advanceTimersByTime(3_000);
    });

    expect(api.request).toHaveBeenCalledWith("/api/v1/ops/view-as/va-1/end", {
      method: "POST",
    });
    expect(api.setAuth).toHaveBeenCalledWith({ orgId: "operator-org" });
    expect(loadViewAs({ includeExpired: true })).toBeNull();
    expect(assign).toHaveBeenCalledWith("/ops");
  });
});
