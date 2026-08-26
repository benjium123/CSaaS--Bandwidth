import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LoginPage } from "./LoginPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const ME = { id: "u1", email: "a@example.com", full_name: "A", memberships: [] };

describe("LoginPage", () => {
  it("logs in and stores the token", async () => {
    const client = makeStubClient({
      "/api/v1/auth/login": { access_token: "tok-1", requires_2fa: false, pending_token: null },
      "/api/v1/auth/me": ME,
    });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<LoginPage />, client);

    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(client.auth.token).toBe("tok-1"));
  });

  it("renders the code step when the server requires 2FA, then verifies", async () => {
    const client = makeStubClient({
      "/api/v1/auth/login": {
        access_token: null,
        requires_2fa: true,
        pending_token: "pending-xyz",
      },
      "/api/v1/auth/2fa/verify": { access_token: "real-token" },
      "/api/v1/auth/me": ME,
    });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<LoginPage />, client);

    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    // The password step alone must NOT be a login.
    const codeField = await screen.findByLabelText("Authenticator code");
    expect(client.auth.token).toBeNull();

    await userEvent.type(codeField, "123456");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));

    await waitFor(() => expect(client.auth.token).toBe("real-token"));
    const verifyCall = client.calls.find((c) => c.path.includes("2fa/verify"));
    expect(verifyCall?.init.json).toEqual({ pending_token: "pending-xyz", code: "123456" });
  });

  it("shows the server message on a bad password", async () => {
    const client = makeStubClient({
      "/api/v1/auth/login": new Error("Incorrect email or password"),
    });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<LoginPage />, client);

    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "nope-nope-nope");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password");
  });
});
