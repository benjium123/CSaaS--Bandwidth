import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation } from "react-router-dom";
import { LineDetailPage } from "./LineDetailPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

vi.mock("@/pages/InboxSettingsPage", () => ({ InboxGrantEditor: () => <div>Grant editor</div> }));
vi.mock("@/pages/NumbersPage", () => ({ EmergencyAddressEditor: () => <div>911 editor</div> }));

function Probe() {
  const l = useLocation();
  return <div data-testid="loc">{l.pathname + l.search}</div>;
}

const NUMBER = {
  id: "num-1",
  e164: "+12145550100",
  carrier_campaign_locked: false,
  e911_supported: true,
  purchased_at: null,
  is_active: true,
  number_type: "local",
  status: "active",
  capabilities: {},
  campaign_id: null,
  registration: "approved",
  registration_detail: "",
  inbox_name: "Front desk",
  emergency_status: "missing",
};
const INBOX = {
  id: "i-1",
  number_id: "num-1",
  name: "Front desk",
  color: "#3b82f6",
  e164: "+12145550100",
  my_role: "admin",
  departments: [{ id: "d1", name: "Sales" }],
};

function stubs() {
  return {
    "/api/v1/auth/me": {
      id: "u",
      email: "a@b.c",
      full_name: "A",
      memberships: [{ org_id: "org-1", org_name: "Acme", org_slug: "a", role_name: "owner" }],
      permissions: ["numbers:manage"],
    },
    "/api/v1/numbers": [NUMBER],
    "/api/v1/inboxes": [INBOX],
    "/api/v1/registration/campaigns": [
      {
        id: "c-2",
        name: "Spare",
        brand_id: "b",
        carrier_refs: {},
        last_error: null,
        missing_for_submission: [],
        number_count: 12,
        number_cap: 50,
        status: "approved",
        use_case: "x",
      },
    ],
    "/api/v1/agent/profiles": [],
  };
}

function renderDetail() {
  return renderWithProviders(
    <>
      <LineDetailPage numberId="num-1" />
      <Probe />
    </>,
    makeStubClient(stubs()),
  );
}

describe("LineDetailPage", () => {
  it("opens on Overview with the four cards", async () => {
    renderDetail();
    expect(await screen.findByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    for (const t of ["Texting", "911", "Answered by", "Who can use it"]) {
      expect(screen.getAllByText(t).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("No 911 address").length).toBeGreaterThan(0);
    expect(screen.getByText("Sales")).toBeInTheDocument();
  });

  it("switches tabs and mirrors them in ?tab=", async () => {
    renderDetail();
    await screen.findByRole("tab", { name: "Overview" });
    await userEvent.click(screen.getByRole("tab", { name: "Who can use it" }));
    expect(await screen.findByText("Grant editor")).toBeInTheDocument();
    expect(screen.getByTestId("loc").textContent).toContain("tab=access");
    await userEvent.click(screen.getByRole("tab", { name: "Calls" }));
    expect(await screen.findByLabelText("Answered by for +12145550100")).toBeInTheDocument();
    expect(screen.getByTestId("loc").textContent).toContain("tab=calls");
    await userEvent.click(screen.getByRole("tab", { name: "Texting" }));
    expect(await screen.findByRole("option", { name: "Spare (12/50)" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    expect(await screen.findByRole("button", { name: "Release" })).toBeInTheDocument();
    expect(screen.getByTestId("loc").textContent).toContain("tab=advanced");
  });

  it("renders no carrier names or costs on any tab", async () => {
    const { container } = renderDetail();
    await screen.findByRole("tab", { name: "Overview" });
    for (const t of ["Overview", "Who can use it", "Calls", "Texting", "Advanced"]) {
      await userEvent.click(screen.getByRole("tab", { name: t }));
      expect(container.textContent).not.toMatch(/telnyx|bandwidth|twilio|plivo|signalwire|\$\d|cost|spend/i);
    }
  });
});
