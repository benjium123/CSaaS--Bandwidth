import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { loadViewAs } from "@/auth/viewAs";
import { ViewAsButton } from "@/components/ops/ViewAsButton";

type Me = { operator_permissions?: string[] } | null;

const api = {
  auth: { orgId: "operator-org" as string | null },
  setAuth: vi.fn(),
  request: vi.fn(),
};

let me: Me = { operator_permissions: ["ops:support"] };

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ api, me }),
}));

const originalLocation = window.location;
const assign = vi.fn();

beforeEach(() => {
  me = { operator_permissions: ["ops:support"] };
  api.auth.orgId = "operator-org";
  api.request.mockReset();
  api.setAuth.mockReset();
  sessionStorage.clear();
  assign.mockReset();
  // jsdom's window.location is a real Location; swap the whole object so the component's
  // window.location.assign call lands on the mock instead of attempting a navigation.
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: { assign },
  });
});

afterEach(() => {
  cleanup();
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: originalLocation,
  });
});

describe("ViewAsButton", () => {
  it("renders nothing when the operator lacks ops:support", () => {
    me = { operator_permissions: ["billing:read"] };
    render(<ViewAsButton orgId="org-1" orgName="Acme" />);
    expect(screen.queryByRole("button", { name: "View as workspace" })).toBeNull();
  });

  it("renders nothing when there is no operator", () => {
    me = null;
    render(<ViewAsButton orgId="org-1" orgName="Acme" />);
    expect(screen.queryByRole("button", { name: "View as workspace" })).toBeNull();
  });

  it("starts a view, stores the grant and goes to the inbox", async () => {
    const user = userEvent.setup();
    api.request.mockResolvedValue({
      id: "va-1",
      org_id: "org-1",
      org_name: "Acme Corp",
      expires_at: "2999-01-01T00:00:00.000Z",
      permissions: ["read"],
    });

    render(<ViewAsButton orgId="org-1" orgName="Acme" />);
    await user.click(screen.getByRole("button", { name: "View as workspace" }));

    expect(api.request).toHaveBeenCalledWith("/api/v1/ops/view-as", {
      method: "POST",
      json: { org_id: "org-1" },
    });
    expect(loadViewAs()).toEqual({
      id: "va-1",
      orgId: "org-1",
      orgName: "Acme Corp",
      expiresAt: "2999-01-01T00:00:00.000Z",
      permissions: ["read"],
      prevOrgId: "operator-org",
    });
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/inbox"));
  });

  it("surfaces the server error", async () => {
    const user = userEvent.setup();
    api.request.mockRejectedValue(new Error("ops role required"));

    render(<ViewAsButton orgId="org-1" orgName="Acme" />);
    await user.click(screen.getByRole("button", { name: "View as workspace" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("ops role required");
    expect(assign).not.toHaveBeenCalled();
  });
});
