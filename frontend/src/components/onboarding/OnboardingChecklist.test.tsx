import * as React from "react";
import { describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, type Me } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";
import { OnboardingChecklist } from "./OnboardingChecklist";

const ME: Me = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
};

function renderChecklist(
  capabilities: unknown,
  ui: React.ReactNode = <OnboardingChecklist />,
) {
  const client = makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/me/capabilities": capabilities,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  return {
    client,
    ...render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider client={client}>
          <MemoryRouter>{ui}</MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    ),
  };
}

describe("OnboardingChecklist", () => {
  it("renders nothing when the capabilities call fails", async () => {
    renderChecklist(new Error("no capabilities"));

    await waitFor(() => {
      expect(screen.queryByLabelText("Setup steps")).not.toBeInTheDocument();
    });
    expect(screen.queryByRole("list", { name: "Setup steps" })).not.toBeInTheDocument();
  });

  it("renders nothing when everything is done", () => {
    renderChecklist({
      permissions: ["settings:read"],
      org: {
        has_provider: true,
        has_number: true,
        member_count: 3,
        registration_state: "approved",
      },
    });

    expect(screen.queryByLabelText("Setup steps")).not.toBeInTheDocument();
  });

  it("renders the four steps and marks completed ones", async () => {
    renderChecklist({
      permissions: ["settings:read"],
      org: {
        has_provider: true,
        has_number: false,
        member_count: 1,
        registration_state: "none",
      },
    });

    const list = await screen.findByRole("list", { name: "Setup steps" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);

    expect(
      screen.getByRole("listitem", { name: "Connect a provider done" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Get a phone number" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Invite your team" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Register for texting" })).toBeInTheDocument();
  });

  it("clicking Get a phone number navigates to /settings/numbers", async () => {
    renderChecklist(
      {
        permissions: ["settings:read"],
        org: {
          has_provider: true,
          has_number: false,
          member_count: 1,
          registration_state: "none",
        },
      },
      <Routes>
        <Route path="/" element={<OnboardingChecklist />} />
        <Route path="/settings/numbers" element={<div>numbers page probe</div>} />
      </Routes>,
    );

    await screen.findByRole("list", { name: "Setup steps" });
    await userEvent.click(screen.getByRole("button", { name: "Get a phone number" }));

    expect(await screen.findByText("numbers page probe")).toBeInTheDocument();
  });
});
