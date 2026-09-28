import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { ApiError } from "@/api/client";
import { LinesPage, lineStatus } from "./LinesPage";
import type { NumberOut } from "@/api/numbers";
import { makeStubClient, renderWithProviders } from "@/test/harness";

vi.mock("@/components/numbers/PortingPanel", () => ({ PortingPanel: () => <div>Porting</div> }));

function Probe() {
  const l = useLocation();
  return <div data-testid="loc">{l.pathname + l.search}</div>;
}

function num(o: Record<string, unknown> = {}) {
  return {
    id: "num-1",
    e164: "+12145550100",
    carrier_campaign_locked: false,
    e911_supported: false,
    purchased_at: null,
    is_active: true,
    number_type: "local",
    status: "active",
    capabilities: {},
    campaign_id: null,
    registration: "approved",
    registration_detail: "",
    inbox_name: null,
    ...o,
  };
}
function inbox(id: string, numberId: string, name: string, e164: string, departments?: { id: string; name: string }[]) {
  return { id, number_id: numberId, name, color: "#3b82f6", e164, my_role: "admin", departments };
}

const NUMBERS = [
  num({ id: "n-a", e164: "+12145550101", campaign_id: "c-1" }),
  num({ id: "n-b", e164: "+12145550102", registration: "pending" }),
  num({ id: "n-c", e164: "+12145550103", e911_supported: true, emergency_status: "missing", registration: "rejected" }),
  num({ id: "n-d", e164: "+12145550104", status: "pending", e911_supported: true, emergency_status: "missing" }),
  num({ id: "n-e", e164: "+12145550105" }),
];
const INBOXES = [
  inbox("i-a", "n-a", "Sales A", "+12145550101", [{ id: "d-s", name: "Sales" }]),
  inbox("i-b", "n-b", "Support B", "+12145550102", [{ id: "d-h", name: "Help" }]),
  inbox("i-c", "n-c", "Sales C", "+12145550103", [{ id: "d-s", name: "Sales" }]),
  inbox("i-d", "n-d", "Loose D", "+12145550104"),
];
const camp = (id: string, name: string, used: number) => ({
  id,
  name,
  brand_id: "b",
  carrier_refs: {},
  last_error: null,
  missing_for_submission: [],
  number_count: used,
  number_cap: 50,
  status: "approved",
  use_case: "x",
});
const CAMPAIGNS = [camp("c-1", "Main", 50), camp("c-2", "Spare", 12), camp("c-3", "Packed", 50)];

function stubs(over: Record<string, unknown> = {}) {
  return {
    "/api/v1/auth/me": {
      id: "u",
      email: "a@b.c",
      full_name: "A",
      memberships: [{ org_id: "org-1", org_name: "Acme", org_slug: "a", role_name: "owner" }],
    },
    "/api/v1/numbers": NUMBERS,
    "/api/v1/inboxes": INBOXES,
    "/api/v1/registration/campaigns": CAMPAIGNS,
    "/api/v1/agent/profiles": [],
    ...over,
  };
}

describe("LinesPage", () => {
  it("groups lines by department with No department last", async () => {
    renderWithProviders(<LinesPage />, makeStubClient(stubs()));
    await screen.findByText("Sales A");
    const groups = screen.getAllByRole("region").map((r) => r.getAttribute("aria-label")).filter(Boolean);
    expect(groups).toEqual(["Help", "Sales", "No department"]);
    const sales = screen.getByRole("region", { name: "Sales" });
    expect(within(sales).getByText("Sales A")).toBeInTheDocument();
    expect(within(sales).getByText("Sales C")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "No department" })).getByText("Loose D")).toBeInTheDocument();
  });

  it("picks one status by precedence", () => {
    const n = (o: Record<string, unknown>) => num(o) as unknown as NumberOut;
    expect(lineStatus(n({ status: "failed", registration: "rejected" })).label).toBe("Order failed");
    expect(lineStatus(n({ status: "pending", e911_supported: true, emergency_status: "missing" })).label).toBe("Order pending");
    expect(lineStatus(n({ e911_supported: true, emergency_status: "missing", registration: "pending" })).label).toBe("No 911 address");
    expect(lineStatus(n({ registration: "rejected" })).label).toBe("Can't text");
    expect(lineStatus(n({ registration: "pending" })).label).toBe("Texting pending");
    expect(lineStatus(n({})).label).toBe("Ready");
  });

  it("renders one status pill per row", async () => {
    renderWithProviders(<LinesPage />, makeStubClient(stubs()));
    await screen.findByText("Sales C");
    const row = screen.getByText("Sales C").closest("li")!;
    expect(within(row).getByText("No 911 address")).toBeInTheDocument();
    expect(within(row).queryByText("Can't text")).toBeNull();
    const rowD = screen.getByText("Loose D").closest("li")!;
    expect(within(rowD).getByText("Order pending")).toBeInTheDocument();
    expect(within(rowD).queryByText("No 911 address")).toBeNull();
  });

  it("campaign picker shows used/cap and disables full campaigns except the current one", async () => {
    renderWithProviders(<LinesPage />, makeStubClient(stubs()));
    await screen.findByText("Sales A");
    const own = screen.getByLabelText("Campaign for +12145550101");
    expect(within(own).getByRole("option", { name: /Main \(50\/50\)/ })).not.toBeDisabled();
    expect(within(own).getByRole("option", { name: /Packed \(50\/50\)/ })).toBeDisabled();
    expect(within(own).getByRole("option", { name: "Spare (12/50)" })).not.toBeDisabled();
  });

  it("a carrier-side campaign can be moved but not cleared", async () => {
    const locked = NUMBERS.map((n) =>
      n.id === "n-a" ? { ...n, carrier_campaign_locked: true } : n,
    );
    renderWithProviders(<LinesPage />, makeStubClient(stubs({ "/api/v1/numbers": locked })));
    await screen.findByText("Sales A");
    const own = screen.getByLabelText("Campaign for +12145550101");
    expect(own).not.toBeDisabled();
    expect(within(own).getByRole("option", { name: "No campaign" })).toBeDisabled();
    expect(within(own).getByRole("option", { name: "Spare (12/50)" })).not.toBeDisabled();
  });

  it("shows the 409 campaign_full message", async () => {
    const client = makeStubClient(
      stubs({
        "/api/v1/numbers/n-b/campaign": () =>
          new ApiError(409, "campaign_full", "That campaign is full (50 of 50 numbers)."),
      }),
    );
    renderWithProviders(<LinesPage />, client);
    await screen.findByText("Support B");
    await userEvent.selectOptions(screen.getByLabelText("Campaign for +12145550102"), "c-2");
    expect(await screen.findByText("That campaign is full (50 of 50 numbers).")).toBeInTheDocument();
  });

  it("clicking a line opens its detail via ?line=", async () => {
    renderWithProviders(
      <>
        <LinesPage />
        <Probe />
      </>,
      makeStubClient(stubs()),
    );
    await screen.findByText("Sales A");
    await userEvent.click(screen.getAllByRole("button", { name: /Open line/ })[0]);
    expect(screen.getByTestId("loc").textContent).toMatch(/line=/);
  });

  it("filters to lines that need attention", async () => {
    renderWithProviders(<LinesPage />, makeStubClient(stubs()));
    await screen.findByText("Sales A");
    await userEvent.click(screen.getByRole("button", { name: "Needs attention" }));
    expect(screen.queryByText("Not in an inbox")).toBeNull();
    expect(screen.getByText("Sales C")).toBeInTheDocument();
  });

  it("never renders carrier names or costs", async () => {
    const { container } = renderWithProviders(<LinesPage />, makeStubClient(stubs()));
    await screen.findByText("Sales A");
    expect(container.textContent).not.toMatch(/telnyx|bandwidth|twilio|plivo|signalwire|\$\d|cost|spend/i);
  });
});
