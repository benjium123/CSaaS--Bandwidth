/**
 * P29: the call result on the inbox timeline's call card.
 *
 * The timeline payload carries no saved result, so the card reads it from ONE calls-list
 * request for the contact - made only once an ended human call is on screen. An assistant
 * call keeps its own card (AiCallCard) and gets no picker.
 */
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import { Timeline } from "./Timeline";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { SoftphoneProvider } from "@/softphone/SoftphoneProvider";

const OUR = "+14694617576";
const CONTACT = "+19725550199";
const TIMELINE = "/api/v1/conversations/%2B19725550199/timeline";

const ME = {
  id: "u1",
  email: "agent@example.com",
  full_name: "Agent Person",
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org",
      org_slug: "org",
      role_name: "admin",
      permissions: ["calls:read", "settings:read"],
    },
  ],
};

function humanCall(overrides: Record<string, unknown> = {}) {
  return {
    kind: "call",
    id: "call1",
    direction: "outbound",
    status: "completed",
    duration_seconds: 42,
    occurred_at: new Date().toISOString(),
    answered_at: null,
    ended_at: null,
    failure_detail: null,
    recording: null,
    has_voicemail: false,
    ...overrides,
  };
}

function routes(items: unknown[]) {
  return {
    [TIMELINE]: { items, next_cursor: null },
    "/api/v1/auth/me": ME,
    "/api/v1/me/capabilities": {
      permissions: ["calls:read", "settings:read"],
      org: { has_provider: false, has_number: false, member_count: 1, registration_state: "none" },
    },
    "/api/v1/orgs/current/calling": {
      recording_announcement: false,
      recording_announcement_text: null,
      announcement_text_effective: "This call may be recorded for quality and training.",
      channel_layout: "mixed",
      dispositions: ["Hot lead", "Not now"],
    },
    "/api/v1/inboxes": [
      { id: "i1", name: "Sales", color: "#22c55e", e164: OUR, number_id: "n1", my_role: "admin" },
    ],
    // Before the generic "/api/v1/calls" stub - the stub client matches by prefix.
    "/api/v1/calls/dispositions": { dispositions: ["Hot lead", "Not now"] },
    "/api/v1/calls": [
      {
        id: "call1",
        direction: "outbound",
        contact_e164: CONTACT,
        our_e164: OUR,
        carrier: "telnyx",
        status: "completed",
        tag: null,
        answered_at: null,
        ended_at: null,
        duration_seconds: 42,
        created_at: new Date().toISOString(),
        disposition: "Hot lead",
        disposition_note: "Call back Tuesday",
      },
    ],
  };
}

function renderTimeline(client: ReturnType<typeof makeStubClient>) {
  return renderWithProviders(
    <SoftphoneProvider>
      <Timeline contactE164={CONTACT} ourE164={OUR} />
    </SoftphoneProvider>,
    client,
  );
}

describe("P29 timeline call result", () => {
  it("shows an ended human call's saved result, editable for someone who can use the number", async () => {
    const client = makeStubClient(routes([humanCall()]));
    renderTimeline(client);

    const group = await screen.findByRole("group", { name: "Call result" });
    expect(within(group).getByText("Hot lead")).toBeInTheDocument();
    expect(within(group).getByText("Call back Tuesday")).toBeInTheDocument();
    expect(await within(group).findByRole("button", { name: "Change" })).toBeInTheDocument();
    expect(
      client.calls.some((c) => c.path.startsWith("/api/v1/calls?") && c.path.includes("contact_e164")),
    ).toBe(true);
  });

  it("does not read the calls list while no human call has ended", async () => {
    const client = makeStubClient(routes([humanCall({ status: "bridged" })]));
    renderTimeline(client);

    expect(await screen.findByText("You called")).toBeInTheDocument();
    // Give any stray query a chance to fire before asserting it did not.
    await waitFor(() => expect(client.calls.some((c) => c.path.startsWith(TIMELINE))).toBe(true));
    expect(client.calls.some((c) => c.path.startsWith("/api/v1/calls?"))).toBe(false);
    expect(screen.queryByRole("group", { name: "Call result" })).not.toBeInTheDocument();
  });

  it("gives an assistant-handled call no picker", async () => {
    const client = makeStubClient(
      routes([
        humanCall({
          id: "call1",
          assistant: {
            name: "Front desk",
            summary: "Asked about pricing.",
            disposition: "handoff",
            sentiment: "positive",
            has_transcript: false,
          },
        }),
      ]),
    );
    renderTimeline(client);

    expect(await screen.findByText("Asked about pricing.")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Call result" })).not.toBeInTheDocument();
  });
});
