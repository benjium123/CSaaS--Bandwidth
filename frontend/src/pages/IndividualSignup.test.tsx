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

describe("SignUpPage — an individual account is a first-class choice", () => {
  it("submits a personal address for an individual account, with no company and no hint", async () => {
    const client = signupClient();
    renderWithProviders(<SignUpPage />, client);

    await userEvent.click(screen.getByRole("radio", { name: "Individual" }));

    // Company is a business field, so it is GONE on this path... (it is asserted present on
    // the business path in the next describe block).
    expect(screen.queryByRole("textbox", { name: "Company" })).toBeNull();

    await userEvent.type(screen.getByLabelText("Email"), "someone@gmail.com");

    // ...and a personal address is allowed, so the consumer-domain courtesy does not fire.
    expect(screen.queryByText(/looks like a personal address/)).toBeNull();

    await userEvent.type(screen.getByLabelText("Your name"), "Someone");
    await userEvent.type(screen.getByLabelText("Password"), PASSPHRASE);

    await userEvent.click(screen.getByRole("button", { name: "Create account" }));

    const call = await registerCall(client);
    expect(call.init.json).toEqual({
      email: "someone@gmail.com",
      password: PASSPHRASE,
      full_name: "Someone",
      company_name: "",
      account_type: "individual",
    });
  });
});

describe("SignUpPage — business stays the default and keeps its courtesy", () => {
  it("defaults to a company account and still warns on a personal address", async () => {
    const client = signupClient();
    renderWithProviders(<SignUpPage />, client);

    // The default is business: the Company radio is checked and its field is present.
    expect(
      (screen.getByRole("radio", { name: "Company" }) as HTMLInputElement).checked,
    ).toBe(true);
    expect(screen.getByRole("radio", { name: "Individual" })).toBeTruthy();
    expect(screen.getByRole("textbox", { name: "Company" })).toBeTruthy();

    await userEvent.type(screen.getByLabelText("Work email"), "someone@gmail.com");
    // The courtesy still fires on the business path.
    expect(screen.getByText(/looks like a personal address/)).toBeTruthy();

    await userEvent.type(screen.getByLabelText("Your name"), "Someone");
    await userEvent.type(screen.getByRole("textbox", { name: "Company" }), "Acme");
    await userEvent.type(screen.getByLabelText("Password"), PASSPHRASE);

    await userEvent.click(screen.getByRole("button", { name: "Create account" }));

    const call = await registerCall(client);
    expect(call.init.json).toEqual({
      email: "someone@gmail.com",
      password: PASSPHRASE,
      full_name: "Someone",
      company_name: "Acme",
      account_type: "business",
    });
  });
});

describe("SignUpPage — switching to individual discards a typed company", () => {
  it("sends an empty company_name even after a company was entered", async () => {
    const client = signupClient();
    renderWithProviders(<SignUpPage />, client);

    await userEvent.type(screen.getByRole("textbox", { name: "Company" }), "Acme Ltd");
    // The switch removes the field entirely; the state underneath could still hold
    // "Acme Ltd", which is exactly what this test exists to prove is NOT sent.
    await userEvent.click(screen.getByRole("radio", { name: "Individual" }));
    expect(screen.queryByRole("textbox", { name: "Company" })).toBeNull();

    await userEvent.type(screen.getByLabelText("Email"), "me@gmail.com");
    await userEvent.type(screen.getByLabelText("Your name"), "Me");
    await userEvent.type(screen.getByLabelText("Password"), "a different long passphrase");

    await userEvent.click(screen.getByRole("button", { name: "Create account" }));

    const call = await registerCall(client);
    expect(call.init.json).toMatchObject({
      account_type: "individual",
      company_name: "",
      full_name: "Me",
    });
  });
});
