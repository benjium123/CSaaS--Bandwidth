import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/api/client";
import type { Me } from "@/auth/AuthContext";
import { SettingsSecurityPage } from "./SettingsSecurityPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const ME_2FA_ON: Me = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
  totp_enabled: true,
};

const ME_2FA_OFF: Me = { ...ME_2FA_ON, totp_enabled: false };

describe("SettingsSecurityPage", () => {
  it("renders the provisioning URI as a link and a copyable field after enrolling", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_OFF,
      "/api/v1/auth/2fa/enroll": {
        secret: "SECRET123",
        provisioning_uri: "otpauth://totp/CSaaS:a@example.com?secret=SECRET123",
      },
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await userEvent.click(screen.getByRole("button", { name: "Set up two-factor authentication" }));

    const link = await screen.findByRole("link", {
      name: "otpauth://totp/CSaaS:a@example.com?secret=SECRET123",
    });
    expect(link).toHaveAttribute("href", "otpauth://totp/CSaaS:a@example.com?secret=SECRET123");
    expect(screen.getByLabelText("Provisioning URI")).toHaveValue(
      "otpauth://totp/CSaaS:a@example.com?secret=SECRET123",
    );
  });

  it("copies the provisioning URI and shows a brief confirmation", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_OFF,
      "/api/v1/auth/2fa/enroll": {
        secret: "SECRET123",
        provisioning_uri: "otpauth://totp/CSaaS:a@example.com?secret=SECRET123",
      },
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await userEvent.click(screen.getByRole("button", { name: "Set up two-factor authentication" }));
    await screen.findByLabelText("Provisioning URI");

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledWith("otpauth://totp/CSaaS:a@example.com?secret=SECRET123");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  // Item 8: the Disable-2FA panel must not appear at all when 2FA isn't on - including
  // the undefined case (backend hasn't shipped `totp_enabled` for this user yet).
  it("hides the disable-2FA panel when totp_enabled is false or missing", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": ME_2FA_OFF });
    renderWithProviders(<SettingsSecurityPage />, client);

    await waitFor(() => expect(client.calls.some((c) => c.path === "/api/v1/auth/me")).toBe(true));
    expect(screen.queryByText("Disable two-factor authentication")).toBeNull();
    expect(screen.queryByLabelText("Confirmation code")).toBeNull();
  });

  it("hides the disable-2FA panel when /auth/me doesn't send totp_enabled at all", async () => {
    const { totp_enabled: _drop, ...meWithoutField } = ME_2FA_ON;
    const client = makeStubClient({ "/api/v1/auth/me": meWithoutField });
    renderWithProviders(<SettingsSecurityPage />, client);

    await waitFor(() => expect(client.calls.some((c) => c.path === "/api/v1/auth/me")).toBe(true));
    expect(screen.queryByText("Disable two-factor authentication")).toBeNull();
  });

  it("disables 2FA with just a code when the user leaves the password field blank", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_ON,
      "/api/v1/auth/2fa/disable": {},
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await userEvent.type(await screen.findByLabelText("Confirmation code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Disable 2FA" }));

    await waitFor(() => expect(screen.getByText("Two-factor authentication is off.")).toBeInTheDocument());
    const call = client.calls.find((c) => c.path === "/api/v1/auth/2fa/disable");
    expect(call?.init.json).toEqual({ code: "123456" });
  });

  it("sends the password when the user fills it in, and surfaces a 422 error otherwise", async () => {
    let attempt = 0;
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_ON,
      "/api/v1/auth/2fa/disable": () => {
        attempt += 1;
        if (attempt === 1) {
          throw new ApiError(422, "password_required", "A password is required to disable 2FA");
        }
        return {};
      },
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await userEvent.type(await screen.findByLabelText("Confirmation code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Disable 2FA" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("A password is required to disable 2FA");
    // The password field was visible all along (not gated on the error) - fill it in and retry.
    const firstCall = client.calls.find((c) => c.path === "/api/v1/auth/2fa/disable");
    expect(firstCall?.init.json).toEqual({ code: "123456" });

    await userEvent.type(screen.getByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Disable 2FA" }));

    await waitFor(() => expect(screen.getByText("Two-factor authentication is off.")).toBeInTheDocument());
    const secondCall = client.calls.filter((c) => c.path === "/api/v1/auth/2fa/disable")[1];
    expect(secondCall?.init.json).toEqual({ code: "123456", password: "hunter2" });
  });

  it("disables the disable-2FA button and its inputs while the request is pending", async () => {
    let rejectRequest: ((err: Error) => void) | undefined;
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_ON,
      "/api/v1/auth/2fa/disable": () =>
        new Promise((_resolve, reject) => {
          rejectRequest = reject;
        }),
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    const codeField = await screen.findByLabelText("Confirmation code");
    await userEvent.type(codeField, "123456");
    const disableButton = screen.getByRole("button", { name: "Disable 2FA" });
    await userEvent.click(disableButton);

    await waitFor(() => expect(disableButton).toBeDisabled());
    expect(codeField).toBeDisabled();

    rejectRequest?.(new Error("network error"));
    await waitFor(() => expect(disableButton).not.toBeDisabled());
    expect(codeField).not.toBeDisabled();
  });
});
