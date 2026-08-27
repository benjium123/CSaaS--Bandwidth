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

  it("tells the visitor plainly when the link has no token", () => {
    window.history.pushState({}, "", "/accept-invite");

    const client = makeStubClient({});
    renderWithProviders(<AcceptInvitePage />, client);

    expect(screen.getByText(/missing its token/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /sign in/i })).toBeInTheDocument();
  });
});
