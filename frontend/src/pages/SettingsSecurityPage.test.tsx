import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/api/client";
import type { Me } from "@/auth/AuthContext";
import { SettingsSecurityPage } from "./SettingsSecurityPage";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const ORG_SETTINGS = { contact_visibility: "everyone" };

const ME_2FA_ON: Me = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
  totp_enabled: true,
};

const ME_2FA_OFF: Me = { ...ME_2FA_ON, totp_enabled: false };

const ME_NO_SETTINGS_WRITE: Me = {
  ...ME_2FA_ON,
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org",
      org_slug: "org",
      role_name: "agent",
      permissions: ["org:read"],
    },
  ],
};

describe("SettingsSecurityPage", () => {
  it("renders the provisioning URI as a link and a copyable field after enrolling", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_OFF,
      "/api/v1/orgs/current/settings": ORG_SETTINGS,
      "/api/v1/auth/2fa/enroll": {
        secret: "SECRET123",
        provisioning_uri: "otpauth://totp/CSaaS:a@example.com?secret=SECRET123",
      },
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await userEvent.click(
      screen.getByRole("button", { name: "Set up two-factor authentication" }),
    );

    const link = await screen.findByRole("link", {
      name: "otpauth://totp/CSaaS:a@example.com?secret=SECRET123",
    });
    expect(link).toHaveAttribute(
      "href",
      "otpauth://totp/CSaaS:a@example.com?secret=SECRET123",
    );
    expect(screen.getByLabelText("Provisioning URI")).toHaveValue(
      "otpauth://totp/CSaaS:a@example.com?secret=SECRET123",
    );
  });

  it("copies the provisioning URI and shows a brief confirmation", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_OFF,
      "/api/v1/orgs/current/settings": ORG_SETTINGS,
      "/api/v1/auth/2fa/enroll": {
        secret: "SECRET123",
        provisioning_uri: "otpauth://totp/CSaaS:a@example.com?secret=SECRET123",
      },
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await userEvent.click(
      screen.getByRole("button", { name: "Set up two-factor authentication" }),
    );
    await screen.findByLabelText("Provisioning URI");

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledWith(
      "otpauth://totp/CSaaS:a@example.com?secret=SECRET123",
    );
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("hides the disable-2FA panel when totp_enabled is false or missing", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_OFF,
      "/api/v1/orgs/current/settings": ORG_SETTINGS,
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await waitFor(() =>
      expect(client.calls.some((call) => call.path === "/api/v1/auth/me")).toBe(true),
    );
    expect(screen.queryByText("Disable two-factor authentication")).toBeNull();
    expect(screen.queryByLabelText("Confirmation code")).toBeNull();
  });

  it("hides the disable-2FA panel when /auth/me doesn't send totp_enabled at all", async () => {
    const meWithoutField = {
      id: ME_2FA_ON.id,
      email: ME_2FA_ON.email,
      full_name: ME_2FA_ON.full_name,
      memberships: ME_2FA_ON.memberships,
    };
    const client = makeStubClient({
      "/api/v1/auth/me": meWithoutField,
      "/api/v1/orgs/current/settings": ORG_SETTINGS,
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await waitFor(() =>
      expect(client.calls.some((call) => call.path === "/api/v1/auth/me")).toBe(true),
    );
    expect(screen.queryByText("Disable two-factor authentication")).toBeNull();
  });

  it("disables 2FA with just a code when the user leaves the password field blank", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_ON,
      "/api/v1/orgs/current/settings": ORG_SETTINGS,
      "/api/v1/auth/2fa/disable": {},
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await userEvent.type(await screen.findByLabelText("Confirmation code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Disable 2FA" }));

    await waitFor(() =>
      expect(screen.getByText("Two-factor authentication is off.")).toBeInTheDocument(),
    );
    const call = client.calls.find((entry) => entry.path === "/api/v1/auth/2fa/disable");
    expect(call?.init.json).toEqual({ code: "123456" });
  });

  it("sends the password when the user fills it in, and surfaces a 422 error otherwise", async () => {
    let attempt = 0;
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_ON,
      "/api/v1/orgs/current/settings": ORG_SETTINGS,
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

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A password is required to disable 2FA",
    );
    const firstCall = client.calls.find(
      (entry) => entry.path === "/api/v1/auth/2fa/disable",
    );
    expect(firstCall?.init.json).toEqual({ code: "123456" });

    await userEvent.type(screen.getByLabelText("Password"), "hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Disable 2FA" }));

    await waitFor(() =>
      expect(screen.getByText("Two-factor authentication is off.")).toBeInTheDocument(),
    );
    const secondCall = client.calls.filter(
      (entry) => entry.path === "/api/v1/auth/2fa/disable",
    )[1];
    expect(secondCall?.init.json).toEqual({ code: "123456", password: "hunter2" });
  });

  it("disables the disable-2FA button and its inputs while the request is pending", async () => {
    let rejectRequest: ((err: Error) => void) | undefined;
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_ON,
      "/api/v1/orgs/current/settings": ORG_SETTINGS,
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

  it("renders the three contact visibility options with consequence sentences", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_OFF,
      "/api/v1/orgs/current/settings": ORG_SETTINGS,
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    expect(await screen.findByText("Contact visibility")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Everyone" })).toBeChecked();
    expect(
      screen.getByText("Every member of the workspace can see every contact."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/People see contacts owned by them or by anyone on their team\./),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/People see only the contacts they own\. Team leads also see their team's\./),
    ).toBeInTheDocument();
  });

  it("saves a contact visibility change immediately", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_OFF,
      "/api/v1/orgs/current/settings": ((_path, init) => {
        if (init.method === "PATCH") {
          return { ...ORG_SETTINGS, contact_visibility: "department" };
        }
        return ORG_SETTINGS;
      }) as RouteStub,
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await screen.findByRole("radio", { name: "Their team" });
    await userEvent.click(screen.getByRole("radio", { name: "Their team" }));

    await waitFor(() => {
      const call = client.calls.find(
        (entry) => entry.path === "/api/v1/orgs/current/settings" && entry.init.method === "PATCH",
      );
      expect(call?.init.json).toEqual({ contact_visibility: "department" });
    });
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Their team" })).toBeChecked();
  });

  it("reverts the visual selection after a failed visibility update", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_2FA_OFF,
      "/api/v1/orgs/current/settings": ((_path, init) => {
        if (init.method === "PATCH") {
          throw new ApiError(422, "policy_locked", "This workspace cannot change this");
        }
        return ORG_SETTINGS;
      }) as RouteStub,
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    await screen.findByRole("radio", { name: "Their team" });
    await userEvent.click(screen.getByRole("radio", { name: "Their team" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This workspace cannot change this",
    );
    expect(screen.getByRole("radio", { name: "Their team" })).not.toBeChecked();
    expect(screen.getByRole("radio", { name: "Everyone" })).toBeChecked();
  });

  it("disables visibility radios without settings:write", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_NO_SETTINGS_WRITE,
      "/api/v1/orgs/current/settings": ORG_SETTINGS,
    });
    renderWithProviders(<SettingsSecurityPage />, client);

    const theirTeam = await screen.findByRole("radio", { name: "Their team" });
    expect(theirTeam).toBeDisabled();
    expect(theirTeam).toHaveAttribute(
      "title",
      "You don't have permission to change this.",
    );

    await userEvent.click(theirTeam);
    expect(
      client.calls.some(
        (entry) => entry.path === "/api/v1/orgs/current/settings" && entry.init.method === "PATCH",
      ),
    ).toBe(false);
  });
});
