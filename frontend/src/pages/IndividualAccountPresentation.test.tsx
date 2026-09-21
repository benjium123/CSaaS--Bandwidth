import * as React from "react";
import { describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, type Me } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
import { makeStubClient } from "@/test/harness";
import { OnboardingChecklist } from "@/components/onboarding/OnboardingChecklist";

function me(accountType: "business" | "individual"): Me {
  return {
    id: "u1",
    email: "a@example.com",
    full_name: "A",
    permissions: ["settings:read"],
    memberships: [
      {
        org_id: "org-1",
        org_name: "Org",
        org_slug: "org",
        role_name: "owner",
        account_type: accountType,
      },
    ],
  };
}

function renderWithProviders(ui: React.ReactNode, meValue: Me, capabilities: unknown) {
  const client = makeStubClient({
    "/api/v1/auth/me": meValue,
    "/api/v1/me/capabilities": capabilities,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

/** Positive probe: renders the gate and reports when capabilities have resolved. */
function GateProbe() {
  const gate = useGate();
  return (
    <div>
      <span data-testid="gate-loading">{String(gate.isLoading)}</span>
      <span data-testid="gate-account-type">{gate.org?.account_type ?? "none"}</span>
    </div>
  );
}

async function waitForGateResolved(accountType: string) {
  await waitFor(() => {
    expect(screen.getByTestId("gate-loading")).toHaveTextContent("false");
  });
  expect(screen.getByTestId("gate-account-type")).toHaveTextContent(accountType);
}

describe("IndividualAccountPresentation", () => {
  it("individual setup completes with provider + number only", async () => {
    renderWithProviders(
      <>
        <GateProbe />
        <OnboardingChecklist />
      </>,
      me("individual"),
      {
        permissions: ["settings:read"],
        org: {
          has_provider: true,
          has_number: true,
          member_count: 1,
          registration_state: "none",
          account_type: "individual",
        },
      },
    );

    await waitForGateResolved("individual");
    expect(screen.queryByLabelText("Setup steps")).not.toBeInTheDocument();
  });

  it("business setup does not complete without team and registration", async () => {
    renderWithProviders(
      <>
        <GateProbe />
        <OnboardingChecklist />
      </>,
      me("business"),
      {
        permissions: ["settings:read"],
        org: {
          has_provider: true,
          has_number: true,
          member_count: 1,
          registration_state: "none",
          account_type: "business",
        },
      },
    );

    await waitForGateResolved("business");
    const list = await screen.findByRole("list", { name: "Setup steps" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
  });

  it("pending individual setup shows two steps", async () => {
    renderWithProviders(
      <>
        <GateProbe />
        <OnboardingChecklist />
      </>,
      me("individual"),
      {
        permissions: ["settings:read"],
        org: {
          has_provider: false,
          has_number: false,
          member_count: 1,
          registration_state: "none",
          account_type: "individual",
        },
      },
    );

    await waitForGateResolved("individual");
    const list = await screen.findByRole("list", { name: "Setup steps" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("listitem", { name: "Connect a provider" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Get a phone number" })).toBeInTheDocument();
    expect(screen.queryByRole("listitem", { name: "Invite your team" })).not.toBeInTheDocument();
    expect(screen.queryByRole("listitem", { name: "Register for texting" })).not.toBeInTheDocument();
  });

  it("merges membership account_type when server omits it", async () => {
    renderWithProviders(
      <>
        <GateProbe />
        <OnboardingChecklist />
      </>,
      me("individual"),
      {
        permissions: ["settings:read"],
        org: {
          has_provider: true,
          has_number: true,
          member_count: 1,
          registration_state: "none",
        },
      },
    );

    await waitForGateResolved("individual");
    expect(screen.queryByLabelText("Setup steps")).not.toBeInTheDocument();
  });
});
