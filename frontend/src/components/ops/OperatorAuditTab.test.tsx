import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OperatorAuditTab, type OperatorAuditEntry } from "./OperatorAuditTab";

const mocks = vi.hoisted(() => ({ requestMock: vi.fn() }));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ api: { request: mocks.requestMock } }),
}));

function renderTab() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <OperatorAuditTab />
    </QueryClientProvider>,
  );
}

function entry(
  overrides: Partial<OperatorAuditEntry> & Pick<OperatorAuditEntry, "id">,
): OperatorAuditEntry {
  return {
    at: "2026-09-27T10:00:00Z",
    operator_user_id: null,
    operator_email: null,
    operator_role: "admin",
    method: "PUT",
    route: "/api/v1/ops/console/orgs/{org_id}/features/{key}",
    path_params: null,
    org_id: null,
    status_code: 200,
    reason: null,
    ip: null,
    ...overrides,
  };
}

function requestedPaths(): string[] {
  return mocks.requestMock.mock.calls.map((call) => String(call[0]));
}

beforeEach(() => {
  mocks.requestMock.mockReset();
});

describe("OperatorAuditTab", () => {
  it("renders a row with the route parameters filled in, the status pill and the reason", async () => {
    mocks.requestMock.mockResolvedValue({
      entries: [
        entry({
          id: "au-1",
          operator_email: "ops@example.com",
          operator_role: "admin",
          path_params: { org_id: "abc", key: "fax" },
          org_id: "abcd1234-5678-90ab-cdef-1234567890ab",
          status_code: 200,
          reason: "enabled fax",
        }),
      ],
    });

    renderTab();

    expect(await screen.findByText("ops@example.com")).toBeTruthy();
    expect(screen.getByText("PUT /api/v1/ops/console/orgs/abc/features/fax")).toBeTruthy();
    expect(screen.getByText("200")).toBeTruthy();
    expect(screen.getByText("enabled fax")).toBeTruthy();
    // Workspace id is shortened to its first 8 characters.
    expect(screen.getByText("abcd1234")).toBeTruthy();
  });

  it("shows a shared ops token instead of an email for token-driven rows", async () => {
    mocks.requestMock.mockResolvedValue({
      entries: [
        entry({
          id: "au-2",
          operator_email: null,
          operator_role: "ops_token",
          method: "POST",
          route: "/api/v1/ops/console/fixed-costs",
          status_code: 403,
          reason: "Forbidden: operator needs the admin role",
        }),
      ],
    });

    renderTab();

    expect(await screen.findByText("Shared ops token")).toBeTruthy();
    expect(screen.getByText("403")).toBeTruthy();
  });

  it("sends only the filled filters and drops the blank ones", async () => {
    const user = userEvent.setup();
    mocks.requestMock.mockResolvedValue({ entries: [] });

    renderTab();

    await waitFor(() => expect(mocks.requestMock).toHaveBeenCalled());

    await user.type(screen.getByLabelText("Search route"), "features");
    await user.type(screen.getByLabelText("Workspace ID"), "abc");
    await user.click(screen.getByRole("button", { name: "Filter" }));

    await waitFor(() => {
      expect(
        requestedPaths().some(
          (path) => path.includes("q=features") && path.includes("org_id=abc"),
        ),
      ).toBe(true);
    });

    const filtered = requestedPaths().filter((path) => path.includes("q=features"));
    expect(filtered.length).toBeGreaterThan(0);
    for (const path of filtered) {
      expect(path).not.toContain("operator_user_id");
      expect(path).not.toContain("before=");
    }
  });

  it("pages back with the oldest rendered row's timestamp", async () => {
    const user = userEvent.setup();
    const rows: OperatorAuditEntry[] = Array.from({ length: 50 }, (_, index) =>
      entry({
        id: `au-${index}`,
        at: `2026-09-27T10:${String(index).padStart(2, "0")}:00Z`,
      }),
    );
    mocks.requestMock.mockResolvedValue({ entries: rows });

    renderTab();

    const older = await screen.findByRole("button", { name: "Older" });
    await user.click(older);

    const oldest = rows[rows.length - 1].at;
    await waitFor(() => {
      expect(
        requestedPaths()
          .map((path) => decodeURIComponent(path))
          .some((path) => path.includes(`before=${oldest}`)),
      ).toBe(true);
    });

    expect(await screen.findByRole("button", { name: "Newest" })).toBeTruthy();
  });

  it("shows the server error message when the read is refused", async () => {
    mocks.requestMock.mockRejectedValue(
      Object.assign(new Error("Forbidden: operator needs the admin role"), {
        status: 403,
      }),
    );

    renderTab();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toMatch(/forbidden/i);
  });
});
