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

  it("with email codes as the only factor, emails a code at once and signs in with it", async () => {
    const client = makeStubClient({
      "/api/v1/auth/login": {
        access_token: null,
        requires_2fa: true,
        pending_token: "pending-mail",
        methods: ["email"],
      },
      "/api/v1/auth/2fa/email/login/send": { sent: true },
      "/api/v1/auth/2fa/email/login/verify": { access_token: "mail-token" },
      "/api/v1/auth/me": ME,
    });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<LoginPage />, client);

    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    const codeField = await screen.findByLabelText("Email code");
    expect(screen.queryByLabelText("Authenticator code")).toBeNull();
    const sends = client.calls.filter((c) => c.path.endsWith("/2fa/email/login/send"));
    expect(sends.map((c) => c.init.json)).toEqual([{ pending_token: "pending-mail" }]);
    expect(client.auth.token).toBeNull();

    await userEvent.type(codeField, "654321");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));

    await waitFor(() => expect(client.auth.token).toBe("mail-token"));
    const verify = client.calls.find((c) => c.path.endsWith("/2fa/email/login/verify"));
    expect(verify?.init.json).toEqual({ pending_token: "pending-mail", code: "654321" });
  });

  it("with an app and email codes, starts on the app and only emails when asked", async () => {
    const client = makeStubClient({
      "/api/v1/auth/login": {
        access_token: null,
        requires_2fa: true,
        pending_token: "pending-both",
        methods: ["totp", "email"],
      },
      "/api/v1/auth/2fa/email/login/send": { sent: true },
      "/api/v1/auth/me": ME,
    });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<LoginPage />, client);

    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await screen.findByLabelText("Authenticator code");
    expect(client.calls.some((c) => c.path.includes("/2fa/email/"))).toBe(false);

    // Choosing the Email code tab is the ask: it sends once, and only then.
    await userEvent.click(screen.getByRole("tab", { name: "Email code" }));
    expect(await screen.findByLabelText("Email code")).toBeInTheDocument();
    expect(client.calls.filter((c) => c.path.endsWith("/2fa/email/login/send"))).toHaveLength(1);
  });
  async function toSecondStep(login: Record<string, unknown>) {
    const client = makeStubClient({
      "/api/v1/auth/login": { access_token: null, requires_2fa: true, pending_token: "p", ...login },
      "/api/v1/auth/2fa/email/login/send": { sent: true },
      "/api/v1/auth/me": ME,
    });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<LoginPage />, client);
    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    return client;
  }

  it("shows only the factors the account holds: email alone means no tabs and a hint", async () => {
    await toSecondStep({ methods: ["email"], recovery_codes_available: true });
    expect(await screen.findByLabelText("Email code")).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.getByText(/Faster next time/)).toBeInTheDocument();
    expect(screen.queryByText(/not turned on for this account/)).toBeNull();
  });

  it("offers a tab per held factor and no hint when passkey and app are both set", async () => {
    await toSecondStep({ methods: ["totp", "passkey", "email"], recovery_codes_available: true });
    await screen.findByRole("tablist");
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(screen.queryByText(/Faster next time/)).toBeNull();
  });

  it("hides 'Use a recovery code' when the account holds none", async () => {
    await toSecondStep({ methods: ["totp"], recovery_codes_available: false });
    await screen.findByLabelText("Authenticator code");
    expect(screen.queryByRole("button", { name: "Use a recovery code" })).toBeNull();
    expect(screen.getByRole("button", { name: "Lost access to your sign-in methods?" })).toBeInTheDocument();
  });

  it("shows 'Use a recovery code' when the account holds some", async () => {
    await toSecondStep({ methods: ["totp"], recovery_codes_available: true });
    await screen.findByLabelText("Authenticator code");
    expect(screen.getByRole("button", { name: "Use a recovery code" })).toBeInTheDocument();
  });
  it("shows and hides the password with the eye button", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": ME });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<LoginPage />, client);
    const input = screen.getByLabelText("Password") as HTMLInputElement;
    expect(input.type).toBe("password");
    await userEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(input.type).toBe("text");
    await userEvent.click(screen.getByRole("button", { name: "Hide password" }));
    expect(input.type).toBe("password");
  });
});
