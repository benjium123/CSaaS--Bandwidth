import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ApiError, type ApiClient } from "@/api/client";
import { AuthProvider } from "@/auth/AuthContext";
import { SsoCallbackPage } from "./SsoCallbackPage";
import { LoginPage } from "./LoginPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const ME = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [
    { org_id: "org-1", org_name: "Org", org_slug: "acme", role_name: "owner" },
  ],
};

function renderSsoCallbackPage(client: ApiClient, initialEntry: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route path="/auth/sso/callback" element={<SsoCallbackPage />} />
            <Route path="/inbox" element={<div>Inbox</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("SsoCallbackPage", () => {
  it("exchanges the code for a token and signs the user in", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/auth/sso/callback": {
        access_token: "sso-token",
        token_type: "bearer",
        org_id: "org-1",
      },
    });

    renderSsoCallbackPage(client, "/auth/sso/callback?code=abc&state=xyz");

    await screen.findByText("Inbox");
    expect(client.auth.token).toBe("sso-token");
    expect(client.auth.orgId).toBe("org-1");
  });

  it("calls the callback exactly once", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/auth/sso/callback": {
        access_token: "sso-token",
        token_type: "bearer",
        org_id: "org-1",
      },
    });

    renderSsoCallbackPage(client, "/auth/sso/callback?code=abc&state=xyz");

    await screen.findByText("Inbox");
    expect(
      client.calls.filter((call) => call.path.startsWith("/api/v1/auth/sso/callback")),
    ).toHaveLength(1);
  });

  it("reports a failed exchange instead of a blank screen", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/auth/sso/callback": () => {
        throw new ApiError(401, "unauthenticated", "Single sign-on failed");
      },
    });

    renderSsoCallbackPage(client, "/auth/sso/callback?code=abc&state=xyz");

    expect(await screen.findByText("Sign-in failed")).toBeTruthy();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Single sign-on failed");
    expect(screen.getByRole("button", { name: "Back to sign in" })).toBeTruthy();
  });

  it("an identity-provider error never reaches the token exchange", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/auth/sso/callback": () => {
        throw new Error("should not be called");
      },
    });

    renderSsoCallbackPage(
      client,
      "/auth/sso/callback?error=access_denied&error_description=User+declined",
    );

    await screen.findByText(
      "We could not complete that sign-in. Start again from the sign-in page.",
    );
    expect(screen.getByText("User declined")).toBeTruthy();
    expect(
      client.calls.filter((call) => call.path.startsWith("/api/v1/auth/sso/callback")),
    ).toHaveLength(0);
  });
});

describe("LoginPage SSO entry", () => {
  it("offers an SSO hand-off that navigates the browser to the org's start endpoint", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
    });
    client.setAuth({ token: null, orgId: null });

    renderWithProviders(<LoginPage />, client);

    await userEvent.click(screen.getByRole("button", { name: "Sign in with SSO" }));

    expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
    expect(screen.queryByRole("link", { name: "Continue" })).toBeNull();

    await userEvent.type(screen.getByLabelText("Workspace short name"), "acme");

    expect(screen.getByRole("link", { name: "Continue" })).toHaveAttribute(
      "href",
      "/api/v1/auth/sso/acme/start",
    );
  });
});
