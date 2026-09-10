import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Timeline } from "./Timeline";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { SoftphoneProvider } from "@/softphone/SoftphoneProvider";

const TIMELINE_ROUTE = "/api/v1/conversations/%2B19725550199/timeline";

function baseMessage(overrides: Partial<{
  id: string;
  direction: string;
  body: string;
  status: string;
  error_code: string | null;
  failure_reason_public: string | null;
  scheduled_for: string | null;
  clicks: number;
  links: Array<{ code: string; target_url: string; clicks: number }>;
  media: null;
  route_reason: string | null;
  occurred_at: string;
}> = {}) {
  return {
    kind: "message" as const,
    id: "m1",
    direction: "inbound",
    body: "hello",
    media: null,
    status: "sent",
    occurred_at: new Date().toISOString(),
    error_code: null,
    route_reason: null,
    failure_reason_public: null,
    scheduled_for: null,
    clicks: 0,
    links: [],
    ...overrides,
  };
}

describe("Timeline P28 message bubbles", () => {
  it("a failed message shows the plain sentence, never the code", async () => {
    const client = makeStubClient({
      [TIMELINE_ROUTE]: {
        items: [
          baseMessage({
            status: "failed",
            error_code: "4720",
            failure_reason_public: "This number can't receive text messages.",
          }),
        ],
        next_cursor: null,
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+19725550100" />
      </SoftphoneProvider>,
      client,
    );

    await screen.findByText("hello");

    expect(screen.getByRole("alert")).toHaveTextContent(
      "This number can't receive text messages.",
    );
    expect(screen.queryByText(/4720/)).toBeNull();
  });

  it("a tracked link that nobody opened says so", async () => {
    const client = makeStubClient({
      [TIMELINE_ROUTE]: {
        items: [
          baseMessage({
            links: [
              { code: "abc", target_url: "https://example.com/x", clicks: 0 },
            ],
            clicks: 0,
          }),
        ],
        next_cursor: null,
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+19725550100" />
      </SoftphoneProvider>,
      client,
    );

    await screen.findByText("hello");
    expect(screen.getByText("Link not opened yet")).toBeInTheDocument();
  });

  it("one click reads Clicked once", async () => {
    const client = makeStubClient({
      [TIMELINE_ROUTE]: {
        items: [
          baseMessage({
            links: [
              { code: "abc", target_url: "https://example.com/x", clicks: 1 },
            ],
            clicks: 1,
          }),
        ],
        next_cursor: null,
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+19725550100" />
      </SoftphoneProvider>,
      client,
    );

    await screen.findByText("hello");
    expect(screen.getByText("Clicked once")).toBeInTheDocument();
  });

  it("two clicks read Clicked 2×", async () => {
    const client = makeStubClient({
      [TIMELINE_ROUTE]: {
        items: [
          baseMessage({
            links: [
              { code: "abc", target_url: "https://example.com/x", clicks: 2 },
            ],
            clicks: 2,
          }),
        ],
        next_cursor: null,
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+19725550100" />
      </SoftphoneProvider>,
      client,
    );

    await screen.findByText("hello");
    expect(screen.getByText("Clicked 2×")).toBeInTheDocument();
  });

  it("a message with no tracked link says nothing about clicks", async () => {
    const client = makeStubClient({
      [TIMELINE_ROUTE]: {
        items: [baseMessage({ links: [] })],
        next_cursor: null,
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+19725550100" />
      </SoftphoneProvider>,
      client,
    );

    await screen.findByText("hello");
    expect(screen.queryByText(/Clicked|Link not opened/)).toBeNull();
  });

  it("a scheduled message shows when it will go out and can be cancelled", async () => {
    const messageId = "m1";
    const client = makeStubClient({
      [TIMELINE_ROUTE]: {
        items: [
          baseMessage({
            body: "hello later",
            status: "scheduled",
            scheduled_for: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
          }),
        ],
        next_cursor: null,
      },
      [`/api/v1/messages/${messageId}/schedule`]: {},
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+19725550100" />
      </SoftphoneProvider>,
      client,
    );

    await screen.findByText("hello later");
    expect(screen.getByText(/^Scheduled for/)).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Cancel scheduled message" }),
    );

    await waitFor(() =>
      expect(
        client.calls.some((call) => call.path === `/api/v1/messages/${messageId}/schedule`),
      ).toBe(true),
    );

    const cancelCall = client.calls.find(
      (call) => call.path === `/api/v1/messages/${messageId}/schedule`,
    );
    expect(cancelCall?.init.method).toBe("DELETE");
  });

  it("a message already sent has no cancel button", async () => {
    const client = makeStubClient({
      [TIMELINE_ROUTE]: {
        items: [baseMessage({ status: "sent", scheduled_for: null })],
        next_cursor: null,
      },
    });

    renderWithProviders(
      <SoftphoneProvider>
        <Timeline contactE164="+19725550199" ourE164="+19725550100" />
      </SoftphoneProvider>,
      client,
    );

    await screen.findByText("hello");
    expect(
      screen.queryByRole("button", { name: "Cancel scheduled message" }),
    ).toBeNull();
  });
});
