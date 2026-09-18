/**
 * The behaviour the redesigned authentication surfaces added, pinned.
 *
 * The first test here exists because of a gap a reviewer found in an EXISTING test rather
 * than in the code: `P41TrustSafety`'s "offers the passkey when the account has one" only
 * asserts the button renders, and jsdom has no WebAuthn, so once the button became
 * render-and-explain instead of render-or-hide, that assertion passed in exactly the state
 * a person cannot act in. It could no longer tell "offered and usable" from "offered but
 * dead". Nothing in the suite covered the usable path at all. This file covers it, so a
 * regression that disabled the passkey button in every browser would be caught.
 */
import { afterEach, describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/api/client";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { LoginPage } from "@/pages/LoginPage";

/** jsdom ships no WebAuthn, so `passkeysSupported()` is false unless we say otherwise. */
function withWebAuthn() {
  (window as unknown as { PublicKeyCredential: unknown }).PublicKeyCredential = function () {};
}

afterEach(() => {
  delete (window as unknown as { PublicKeyCredential?: unknown }).PublicKeyCredential;
});

async function signInTo2fa(methods: string[]) {
  const client = makeStubClient({
    "/api/v1/auth/me": new Error("401"),
    "/api/v1/auth/login": {
      access_token: null,
      requires_2fa: true,
      pending_token: "pending-1",
      methods,
    },
  });
  client.setAuth({ token: null, orgId: null });
  renderWithProviders(<LoginPage />, client);
  await userEvent.type(screen.getByLabelText("Email"), "a@b.test");
  await userEvent.type(screen.getByLabelText("Password"), "correct-horse-battery");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
  return client;
}

describe("LoginPage second factor", () => {
  it("leaves the passkey control usable when the browser supports WebAuthn", async () => {
    withWebAuthn();
    await signInTo2fa(["passkey"]);

    const button = await screen.findByRole("button", { name: "Use your passkey" });
    expect(button).not.toBeDisabled();
    expect(button).not.toHaveAttribute("aria-disabled", "true");
    expect(screen.queryByText(/This browser cannot use passkeys/)).toBeNull();
  });

  it("keeps the passkey control reachable but inert when the browser has no WebAuthn, and says why", async () => {
    await signInTo2fa(["passkey"]);

    const button = await screen.findByRole("button", { name: "Use your passkey" });
    // aria-disabled, NOT the disabled attribute: the control has to stay in the tab order
    // so the reason is announced to a keyboard or screen-reader user when focus lands.
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).not.toBeDisabled();
    const note = screen.getByText(/This browser cannot use passkeys/);
    expect(note).toBeInTheDocument();
    expect(button.getAttribute("aria-describedby")).toBe(
      note.closest("[id]")?.getAttribute("id"),
    );
  });

  it("offers a way on when the server names no factor this screen can use", async () => {
    await signInTo2fa([]);

    expect(
      await screen.findByText(/needs a second factor that is not available on this screen/),
    ).toBeInTheDocument();
    // The two exits are present and neither restarts the sign-in.
    expect(screen.getByRole("button", { name: "Use a recovery code" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Lost access/ })).toBeInTheDocument();
  });

  it("keeps the pending sign-in when switching to a recovery code", async () => {
    const client = await signInTo2fa(["passkey"]);
    await userEvent.click(await screen.findByRole("button", { name: "Use a recovery code" }));
    await userEvent.type(screen.getByLabelText("Recovery code"), "abcd-efgh-ijkm");
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));

    // The pending token from the FIRST step is what gets sent - the exit did not discard it.
    await waitFor(() => {
      const call = client.calls.find((c) => c.path === "/api/v1/auth/2fa/recovery");
      expect((call?.init.json as { pending_token?: string } | undefined)?.pending_token).toBe(
        "pending-1",
      );
    });
  });
});

describe("LoginPage failure handling", () => {
  it("offers the single sign-on hand-off when the workspace enforces it", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": new Error("401"),
      "/api/v1/auth/login": new ApiError(
        403,
        "sso_required",
        "Your organization requires single sign-on",
      ),
    });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<LoginPage />, client);

    await userEvent.type(screen.getByLabelText("Email"), "a@b.test");
    await userEvent.type(screen.getByLabelText("Password"), "correct-horse-battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    // The server's own message, verbatim, and a route out of a password that will never work.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Your organization requires single sign-on",
    );
    expect(screen.getByLabelText("Workspace short name")).toBeInTheDocument();
  });

  /**
   * The no-branching-on-code property, as a test rather than a promise.
   *
   * `AuthContext` now carries `ApiError.code` on all five sign-in paths, and `code` is
   * allowed to choose an AFFORDANCE (which panel opens) but never the WORDING. A component
   * with no code-keyed variants protects itself, but nothing stops a future
   * `if (code === "account_locked")` appearing in a PAGE - so this pins the page: two
   * different codes, two different server sentences, each rendered verbatim and nothing
   * added. If someone writes a per-cause message, one of these goes red.
   */
  it.each([
    ["account_locked", 423, "Too many attempts. Try again in 14 minutes."],
    ["unauthenticated", 401, "Incorrect email or password"],
  ])("renders the server's own sentence for %s and adds nothing of its own", async (
    code,
    status,
    message,
  ) => {
    const client = makeStubClient({
      "/api/v1/auth/me": new Error("401"),
      "/api/v1/auth/login": new ApiError(status as number, code as string, message as string),
    });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<LoginPage />, client);

    await userEvent.type(screen.getByLabelText("Email"), "a@b.test");
    await userEvent.type(screen.getByLabelText("Password"), "whatever-goes-here");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    // Verbatim, and the ONLY alert: no second sentence explaining what the code meant.
    expect(alert).toHaveTextContent(message as string);
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(alert.textContent?.trim()).toBe(message);
  });

  it("shows the server's words for an ordinary failure and opens nothing extra", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": new Error("401"),
      "/api/v1/auth/login": new ApiError(401, "unauthenticated", "Incorrect email or password"),
    });
    client.setAuth({ token: null, orgId: null });
    renderWithProviders(<LoginPage />, client);

    await userEvent.type(screen.getByLabelText("Email"), "a@b.test");
    await userEvent.type(screen.getByLabelText("Password"), "nope-nope-nope-nope");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password");
    // Nothing on this screen may differentiate a real account from an unknown one: the SSO
    // panel stays shut, and the alert carries the server's sentence and nothing of ours.
    expect(screen.queryByLabelText("Workspace short name")).toBeNull();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });
});
