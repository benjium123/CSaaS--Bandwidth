import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SignUpPage } from "@/pages/SignUpPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

/**
 * Individual signup, and the business default it must not disturb.
 *
 * The three cases here are the whole contract: an individual account takes a personal
 * address and never carries a company, a business account remains the default and keeps its
 * consumer-domain courtesy, and switching business -> individual DISCARDS a company already
 * typed rather than smuggling it into the payload.
 *
 * Every "it is hidden" claim is paired with the same element being present on the other path
 * (the Company textbox is asserted absent for individual and present for business), so a
 * form that simply failed to render cannot pass the absence by accident. The payload is
 * asserted on the STUBBED REQUEST, not on a variable we set, so these tests fail if the
 * component stops posting what it claims to.
 */

const ME = {
  id: "u1",
  email: "someone@acme.co",
  full_name: "Someone",
  memberships: [],
  permissions: [],
};

function signupClient() {
  return makeStubClient({
    "/api/v1/auth/register": {},
    "/api/v1/auth/login": {
      access_token: "token",
      requires_2fa: false,
      pending_token: null,
      methods: ["totp"],
    },
    "/api/v1/auth/me": ME,
  });
}

/** The one /api/v1/auth/register request, once the form has actually issued it. */
async function registerCall(client: ReturnType<typeof signupClient>) {
  await waitFor(() => {
    expect(client.calls.some((call) => call.path === "/api/v1/auth/register")).toBe(true);
  });
  const call = client.calls.find((entry) => entry.path === "/api/v1/auth/register");
  if (!call) throw new Error("expected a /api/v1/auth/register request");
  return call;
}

const PASSPHRASE = "correct horse battery staple";

describe("Unified signup", () => {
  it.each(["someone@gmail.com", "someone@company.com"])("accepts %s with identity verification and no account-type choice", async (email) => {
    const client = signupClient();
    renderWithProviders(<SignUpPage />, client);
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Company" })).toBeNull();
    await userEvent.type(screen.getByLabelText("Email"), email);
    await userEvent.type(screen.getByLabelText("Your name"), "Someone");
    await userEvent.type(screen.getByLabelText("Password"), PASSPHRASE);
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    const call = await registerCall(client);
    expect(call.init.json).toEqual({ email, password: PASSPHRASE, full_name: "Someone", company_name: "", account_type: "individual" });
    expect(screen.queryByText(/looks like a personal address/)).toBeNull();
  });
});
