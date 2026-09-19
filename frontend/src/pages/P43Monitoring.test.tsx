import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { DecisionPackCard, type DecisionPack } from "@/components/ops/DecisionPackCard";
import { MonitoringBanner } from "@/components/kyc/MonitoringBanner";
import { MonitoringTab } from "@/components/ops/MonitoringTab";
import { OwnerResidence } from "@/components/kyc/OwnerResidence";
import { ReportNumberPage } from "./ReportNumberPage";
import type { KycPerson } from "@/api/kyc";

const ME = {
  id: "u1",
  email: "owner@acme.com",
  full_name: "Owner",
  memberships: [
    {
      org_id: "org-1",
      org_name: "Acme",
      org_slug: "acme",
      role_name: "owner",
      permissions: ["org:read", "org:update"],
    },
  ],
  permissions: ["org:read", "org:update"],
};

const PACK: DecisionPack = {
  recommendation: "approve",
  confidence: 91,
  summary: "Established plumbing company; owner verified; documents match.",
  thoughts: [{ area: "identity", assessment: "ok", thought: "Owner passed ID and selfie." }],
  concerns: [{ concern: "Young website", evidence: "Domain is 120 days old" }],
  questions_for_applicant: [],
  suggested_risk: "standard",
  suggested_limits: { daily_calls: 300, daily_texts: 800, max_numbers: 3 },
  note_for_decision: "Identity and company confirmed.",
};

describe("DecisionPackCard", () => {
  it("shows the AI's review and approves with its suggested limits", async () => {
    const onApprove = vi.fn();
    renderWithProviders(
      <DecisionPackCard
        pack={PACK}
        result="pass"
        blockers={[]}
        pending={false}
        onApprove={onApprove}
        onAskInfo={vi.fn()}
        onReject={vi.fn()}
      />,
      makeStubClient({ "/api/v1/auth/me": ME }),
    );
    expect(screen.getByText("AI recommends: approve")).toBeTruthy();
    expect(screen.getByText("91% confident")).toBeTruthy();
    expect(screen.getByText(/Young website/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Approve as recommended" }));
    expect(onApprove).toHaveBeenCalledWith("Identity and company confirmed.", PACK.suggested_limits);
  });

  it("cannot approve while blockers remain", () => {
    renderWithProviders(
      <DecisionPackCard
        pack={PACK}
        result="pass"
        blockers={["Documents have not finished their automatic review"]}
        pending={false}
        onApprove={vi.fn()}
        onAskInfo={vi.fn()}
        onReject={vi.fn()}
      />,
      makeStubClient({ "/api/v1/auth/me": ME }),
    );
    expect(screen.getByRole("button", { name: "Approve as recommended" })).toBeDisabled();
  });

  it("prefills the questions when more info is recommended", async () => {
    const onAskInfo = vi.fn();
    renderWithProviders(
      <DecisionPackCard
        pack={{ ...PACK, recommendation: "needs_info", questions_for_applicant: ["Upload a clearer utility bill."] }}
        result="warn"
        blockers={[]}
        pending={false}
        onApprove={vi.fn()}
        onAskInfo={onAskInfo}
        onReject={vi.fn()}
      />,
      makeStubClient({ "/api/v1/auth/me": ME }),
    );
    expect(screen.getByDisplayValue("Upload a clearer utility bill.")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Ask for more info as recommended" }));
    expect(onAskInfo).toHaveBeenCalledWith("Upload a clearer utility bill.");
  });
});

describe("OwnerResidence", () => {
  it("uploads a proof of address for that owner and shows why it was not accepted", async () => {
    const person: KycPerson = {
      id: "p1",
      role: "owner",
      full_name: "Jane Smith",
      email: null,
      ownership_percent: 100,
      // Linked to an account, but NOT the viewer - the case that tells `is_user` and
      // `is_you` apart, and the one the heading assertion below pins.
      is_user: true,
      is_you: false,
      status: "verified",
      verified_name: "Jane Smith",
      document_country: "US",
      verified_at: null,
      last_error: null,
      residential_address: { line1: "12 Oak St", city: "Austin", region: "TX", postal_code: "78702", country: "US" },
    };
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/kyc/documents": { id: "d2" },
      "/api/v1/kyc/profile": {},
    });
    renderWithProviders(
      <OwnerResidence
        person={person}
        editable
        documents={[
          {
            id: "d1",
            kind: "proof_of_address",
            filename: "old-bill.pdf",
            content_type: "application/pdf",
            size_bytes: 10,
            uploaded_at: null,
            person_id: "p1",
            review_status: "fail",
            review_message: "The document is older than 90 days.",
          },
        ]}
      />,
      client,
    );
    // Third person, because Jane is not the viewer. Reverting this to `is_user` would
    // address another owner as "you" and send them into her ID session.
    expect(screen.getByText("Where Jane Smith lives now")).toBeTruthy();
    expect(screen.getByText("Not accepted")).toBeTruthy();
    expect(screen.getByText("The document is older than 90 days.")).toBeTruthy();
    const file = new File(["%PDF-1.4"], "bill.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText("Proof of address for Jane Smith"), file);
    await userEvent.click(screen.getByRole("button", { name: "Upload proof" }));
    await waitFor(() => {
      const call = client.calls.find((c) => c.path === "/api/v1/kyc/documents" && c.init.method === "POST");
      const body = call?.init.body as FormData;
      expect(body.get("kind")).toBe("proof_of_address");
      expect(body.get("person_id")).toBe("p1");
    });
  });
});

describe("MonitoringBanner", () => {
  it("explains a pause and sends the business's explanation", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/monitoring/status": { level: "paused", message: "Calling and texting are paused while our team reviews.", appealed_at: null },
      "/api/v1/monitoring/appeal": { appealed_at: "2026-09-17T10:00:00Z" },
    });
    renderWithProviders(<MonitoringBanner />, client);
    expect(await screen.findByText("Calling and texting are paused")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Tell us what happened" }));
    await userEvent.type(
      screen.getByLabelText("Explanation for the review team"),
      "We were sending appointment reminders for our clinic.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Send to the review team" }));
    await waitFor(() => {
      const call = client.calls.find((c) => c.path === "/api/v1/monitoring/appeal");
      expect(call?.init.json).toEqual({ explanation: "We were sending appointment reminders for our clinic." });
    });
  });

  it("stays hidden when everything is normal", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/monitoring/status": { level: "normal", message: null, appealed_at: null },
    });
    renderWithProviders(<MonitoringBanner />, client);
    await waitFor(() => expect(client.calls.some((c) => c.path === "/api/v1/monitoring/status")).toBe(true));
    expect(screen.queryByRole("status", { name: "Account review" })).toBeNull();
  });
});

describe("ReportNumberPage", () => {
  it("sends a report without an account", async () => {
    const client = makeStubClient({ "/api/v1/public/report-number": { received: true } });
    renderWithProviders(<ReportNumberPage />, client);
    await userEvent.type(screen.getByLabelText("Number that contacted you"), "+15125550100");
    await userEvent.type(screen.getByLabelText("What happened"), "They said I owe the IRS and must buy gift cards.");
    await userEvent.click(screen.getByRole("button", { name: "Send report" }));
    expect(await screen.findByText(/your report was received/)).toBeTruthy();
    const call = client.calls.find((c) => c.path === "/api/v1/public/report-number");
    expect(call?.init.json).toMatchObject({ number: "+15125550100", kind: "call" });
  });
});

describe("MonitoringTab", () => {
  it("shows health, flagged accounts and releases a held text", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/ops/monitoring/report": {
        texts: { checked: 120, allowed: 115, held_now: 1, blocked: 4 },
        calls: { reviewed: 30, ok: 28, suspicious: 1, scam: 1, waiting: 2 },
        accounts: { paused: 1 },
        public_reports: 2,
        ai_tokens: { in: 50000, out: 6000 },
        health: { canary: { passed: true, at: "2026-09-17T09:00:00Z", detail: null }, exam: null },
      },
      "/api/v1/ops/monitoring/held-texts": [
        { id: "m1", org_id: "o1", body: "Flash sale on gutters!", to: "+15125550199", reason: "Unusual", at: null },
      ],
      "/api/v1/ops/monitoring/texts/m1": { id: "m1", state: "cleared" },
      "/api/v1/ops/monitoring": [
        { org_id: "o9", org_name: "Scammy LLC", level: "paused", score: 110, paused_at: null, appealed: true, case_status: "ready", recommendation: "suspend_and_ban" },
      ],
    });
    renderWithProviders(<MonitoringTab />, client);
    expect(await screen.findByText(/Canary: passing/)).toBeTruthy();
    expect(screen.getByText("Scammy LLC")).toBeTruthy();
    expect(screen.getByText(/AI: suspend and ban/)).toBeTruthy();
    await userEvent.click(await screen.findByRole("button", { name: "Release" }));
    await waitFor(() => {
      const call = client.calls.find((c) => c.path === "/api/v1/ops/monitoring/texts/m1");
      expect(call?.init.json).toEqual({ decision: "release" });
    });
  });
});
