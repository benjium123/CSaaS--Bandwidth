import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";
import { SettingsProfilePage } from "./SettingsProfilePage";

const baseProfile = {
  full_name: "Ada Lovelace",
  email: "ada@example.com",
  emergency_address: null,
  numbers: [] as { e164: string; status: string | null; mine: boolean }[],
  notice: "",
};

async function fillAddressForm(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.type(screen.getByLabelText("Business or person at this location"), "Ada Lovelace");
  await user.type(screen.getByLabelText("Street address"), "1 Main St");
  await user.type(screen.getByLabelText("City"), "Austin");
  await user.type(screen.getByLabelText("State (2-letter)"), "tx");
  await user.type(screen.getByLabelText("ZIP code"), "78701");
}

describe("SettingsProfilePage", () => {
  it("shows the profile name and an unset emergency address", async () => {
    const client = makeStubClient({
      "/api/v1/me/profile": { ...baseProfile },
    });
    renderWithProviders(<SettingsProfilePage />, client);

    const input = (await screen.findByLabelText("Your name")) as HTMLInputElement;
    expect(input.value).toBe("Ada Lovelace");
    expect(screen.getByText("Not set")).toBeTruthy();
  });

  it("saves the name with a PUT and shows Saved", async () => {
    const user = userEvent.setup();
    let serverName = "Ada Lovelace";
    const handler: RouteStub = (_path, init) => {
      if (init.method === "PUT") {
        serverName = (init.json as { full_name: string }).full_name;
      }
      return { ...baseProfile, full_name: serverName };
    };
    const client = makeStubClient({ "/api/v1/me/profile": handler });
    renderWithProviders(<SettingsProfilePage />, client);

    const input = await screen.findByLabelText("Your name");
    await user.clear(input);
    await user.type(input, "Grace Hopper");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Saved")).toBeTruthy();

    const put = client.calls.find(
      (call) => call.path === "/api/v1/me/profile" && call.init.method === "PUT",
    );
    expect(put?.init.json).toEqual({ full_name: "Grace Hopper" });
  });

  it("saves a new emergency address and reports the applied number count", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      "/api/v1/me/profile": { ...baseProfile },
      "/api/v1/me/emergency-address": { applied: 2 },
    });
    renderWithProviders(<SettingsProfilePage />, client);

    await screen.findByLabelText("Your name");
    await user.click(screen.getByRole("button", { name: "Set" }));
    await fillAddressForm(user);
    await user.click(screen.getByRole("button", { name: "Save address" }));

    expect(await screen.findByText("Address registered on 2 number(s)")).toBeTruthy();

    const put = client.calls.find(
      (call) => call.path === "/api/v1/me/emergency-address" && call.init.method === "PUT",
    );
    expect(put?.init.json).toEqual({
      name: "Ada Lovelace",
      street_address: "1 Main St",
      locality: "Austin",
      administrative_area: "TX",
      postal_code: "78701",
      country_code: "US",
    });
  });

  it("lists the numbers that use the address", async () => {
    const client = makeStubClient({
      "/api/v1/me/profile": {
        ...baseProfile,
        numbers: [
          { e164: "+15125550100", status: "active", mine: true },
          { e164: "+15125550101", status: null, mine: false },
        ],
      },
    });
    renderWithProviders(<SettingsProfilePage />, client);

    expect(await screen.findByText("Numbers using your address")).toBeTruthy();
    expect(screen.getByText("active")).toBeTruthy();
    expect(screen.getByText("not set")).toBeTruthy();
    expect(screen.getByText("(yours)")).toBeTruthy();
  });

  it("shows a message when no numbers are assigned", async () => {
    const client = makeStubClient({
      "/api/v1/me/profile": { ...baseProfile, numbers: [] },
    });
    renderWithProviders(<SettingsProfilePage />, client);

    expect(
      await screen.findByText(
        "No phone numbers are assigned to you yet. Ask an admin to add you to a line.",
      ),
    ).toBeTruthy();
  });

  it("shows an alert when the address save fails", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      "/api/v1/me/profile": { ...baseProfile },
      "/api/v1/me/emergency-address": () => new Error("That ZIP code is not valid for this state."),
    });
    renderWithProviders(<SettingsProfilePage />, client);

    await screen.findByLabelText("Your name");
    await user.click(screen.getByRole("button", { name: "Set" }));
    await fillAddressForm(user);
    await user.click(screen.getByRole("button", { name: "Save address" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBeTruthy();
  });
});
