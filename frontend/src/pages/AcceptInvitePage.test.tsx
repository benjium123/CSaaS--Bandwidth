import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AcceptInvitePage } from "./AcceptInvitePage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

describe("AcceptInvitePage", () => {
  it("submits the token from the query string and surfaces a backend error verbatim", async () => {
    window.history.pushState({}, "", "/accept-invite?token=raw-token-xyz");

    const client = makeStubClient({
      "/api/v1/auth/register": () =>
        new Error("That invitation was issued to a different email address."),
    });
    renderWithProviders(<AcceptInvitePage />, client);

    await userEvent.type(screen.getByLabelText("Email"), "b@x.com");
    await userEvent.type(screen.getByLabelText("Full name"), "B User");
    await userEvent.type(screen.getByLabelText("Password"), "a-long-enough-password");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/auth/register")).toBe(true),
    );
    const registerCall = client.calls.find((c) => c.path === "/api/v1/auth/register");
    expect(registerCall?.init.json).toEqual({
      email: "b@x.com",
      password: "a-long-enough-password",
      full_name: "B User",
      invite_token: "raw-token-xyz",
    });

    expect(
      await screen.findByText("That invitation was issued to a different email address."),
    ).toBeInTheDocument();
  });

  // Item 49: registering into an org that requires 2FA must not silently fail or skip
  // straight past the code step - it should behave exactly like LoginPage's own
  // needs_2fa branch.
  it("shows the 2FA code step when login-after-register requires it, then verifies", async () => {
    window.history.pushState({}, "", "/accept-invite?token=raw-token-xyz");

    const client = makeStubClient({
      "/api/v1/auth/register": {},
      "/api/v1/auth/login": {
        access_token: null,
        requires_2fa: true,
        pending_token: "pending-abc",
      },
      "/api/v1/auth/2fa/verify": { access_token: "real-token" },
      "/api/v1/auth/me": { id: "u1", email: "b@x.com", full_name: "B User", memberships: [] },
    });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<AcceptInvitePage />, client);

    await userEvent.type(screen.getByLabelText("Email"), "b@x.com");
    await userEvent.type(screen.getByLabelText("Full name"), "B User");
    await userEvent.type(screen.getByLabelText("Password"), "a-long-enough-password");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));

    const codeField = await screen.findByLabelText("Authenticator code");
    expect(client.auth.token).toBeNull();

    await userEvent.type(codeField, "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));

    await waitFor(() => expect(client.auth.token).toBe("real-token"));
    const verifyCall = client.calls.find((c) => c.path.includes("2fa/verify"));
    expect(verifyCall?.init.json).toEqual({ pending_token: "pending-abc", code: "123456" });
    // Registration must not be re-submitted on the verify step.
    expect(client.calls.filter((c) => c.path === "/api/v1/auth/register")).toHaveLength(1);
  });

  it("tells the visitor plainly when the link has no token", () => {
    window.history.pushState({}, "", "/accept-invite");

    const client = makeStubClient({});
    renderWithProviders(<AcceptInvitePage />, client);

    expect(screen.getByText(/missing its token/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /sign in/i })).toBeInTheDocument();
  });
});
