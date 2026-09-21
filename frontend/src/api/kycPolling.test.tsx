import { act, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makeStubClient } from "@/test/harness";
import { KYC_POLL_INTERVAL_MS, useKycProfile } from "@/api/kyc";
import type { KycPerson, KycProfile } from "@/api/kyc";

/**
 * Focused test for the webhook-return polling behaviour. We drive the real hook through a
 * real QueryClientProvider and a stubbed ApiClient, and use Vitest fake timers to advance
 * the refetchInterval schedule deterministically. Assertions are on the rendered person
 * status (positive evidence of the transition) AND the request count.
 */

function person(status: KycPerson["status"], id = "p1"): KycPerson {
  return {
    id,
    role: "owner",
    full_name: "Ada Lovelace",
    email: "ada@example.com",
    ownership_percent: 100,
    is_user: true,
    is_you: true,
    status,
    verified_name: null,
    document_country: null,
    verified_at: null,
    last_error: null,
  };
}

function profile(status: KycProfile["status"], persons: KycPerson[]): KycProfile {
  return {
    status,
    account_type: "individual",
    business: {
      country: "US",
      legal_name: "Ada Lovelace",
      dba_name: null,
      entity_type: null,
      registration_number: null,
      tax_id: null,
      incorporation_date: null,
      registered_address: null,
      operating_address: null,
      website: null,
      business_email: null,
      business_phone: null,
    },
    use_case: null,
    use_case_pending: null,
    persons,
    documents: [],
    checks: {},
    agreement: { current_version: "1", accepted_version: null, accepted_at: null },
    missing: [],
    info_request: null,
    submitted_at: null,
    decided_at: null,
    decision_reason: null,
    limits: null,
    deposit_required_cents: null,
    next_reverification_at: null,
  };
}

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
        // The hook's own refetchInterval is the only schedule in play.
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
      },
    },
  });
}

function Probe({
  client,
  enabled = true,
}: {
  client: ReturnType<typeof makeStubClient>;
  enabled?: boolean;
}) {
  const { data } = useKycProfile(client, enabled);
  return (
    <div>
      <span data-testid="profile-status">{data?.status ?? "none"}</span>
      <span data-testid="person-status">{data?.persons?.[0]?.status ?? "none"}</span>
    </div>
  );
}

function renderProbe(client: ReturnType<typeof makeStubClient>, enabled = true) {
  const queryClient = makeQueryClient();
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <Probe client={client} enabled={enabled} />
    </QueryClientProvider>,
  );
  return { ...utils, queryClient };
}

/**
 * Drain React Query's notifyManager batch. After a fetch resolves, React Query schedules
 * its subscriber notification on a zero-delay timer; advancing 1ms inside act lets that
 * callback run - and also flushes the nested zero-time timers Sinon schedules at next+1ms
 * - so the rendered status reflects the just-resolved query.
 */
async function drainNotifications() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
}

/** Flush the initial query and its notifications without real timers. */
async function flush() {
  await drainNotifications();
}

/**
 * Advance one polling interval inside act, then drain the queued notify callbacks so the
 * rendered status is up to date before we assert.
 */
async function tick(ms = KYC_POLL_INTERVAL_MS) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await drainNotifications();
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useKycProfile webhook-return polling", () => {
  it("polls while a person is pending/processing and stops once verified", async () => {
    const responses: KycProfile[] = [
      profile("submitted", [person("pending")]),
      profile("submitted", [person("processing")]),
      profile("submitted", [person("verified")]),
    ];
    let i = 0;
    const client = makeStubClient({
      "/api/v1/kyc/profile": () => responses[Math.min(i++, responses.length - 1)],
    });

    const { getByTestId, queryClient, unmount } = renderProbe(client);

    await flush();
    expect(getByTestId("profile-status").textContent).toBe("submitted");
    expect(getByTestId("person-status").textContent).toBe("pending");
    expect(client.calls.length).toBe(1);

    // pending -> processing
    await tick();
    expect(getByTestId("person-status").textContent).toBe("processing");
    expect(client.calls.length).toBe(2);

    // processing -> verified
    await tick();
    expect(getByTestId("person-status").textContent).toBe("verified");
    expect(client.calls.length).toBe(3);

    // Verified person does NOT imply an approved profile - status stays submitted.
    expect(getByTestId("profile-status").textContent).toBe("submitted");

    // No further polling once no person is in flight.
    await tick(KYC_POLL_INTERVAL_MS * 3);
    expect(client.calls.length).toBe(3);

    unmount();
    queryClient.clear();
  });

  it.each(["verified", "requires_input", "canceled", "not_started"] as const)(
    "does not poll when the first load has a terminal person (%s)",
    async (terminal) => {
      const client = makeStubClient({
        "/api/v1/kyc/profile": profile("submitted", [person(terminal)]),
      });

      const { getByTestId, queryClient, unmount } = renderProbe(client);

      await flush();
      expect(getByTestId("person-status").textContent).toBe(terminal);
      expect(client.calls.length).toBe(1);

      await tick(KYC_POLL_INTERVAL_MS * 3);
      expect(client.calls.length).toBe(1);

      unmount();
      queryClient.clear();
    },
  );

  it("makes no requests when disabled", async () => {
    const client = makeStubClient({
      "/api/v1/kyc/profile": profile("submitted", [person("pending")]),
    });

    const { getByTestId, queryClient, unmount } = renderProbe(client, false);

    await flush();
    expect(getByTestId("profile-status").textContent).toBe("none");
    expect(getByTestId("person-status").textContent).toBe("none");

    await tick(KYC_POLL_INTERVAL_MS * 3);
    expect(client.calls.length).toBe(0);

    unmount();
    queryClient.clear();
  });
});
