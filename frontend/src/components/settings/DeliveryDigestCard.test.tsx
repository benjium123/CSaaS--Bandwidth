import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  DeliveryDigestCard,
  type DeliveryDigestSettings,
} from "./DeliveryDigestCard";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const caps = (permissions: string[]) => ({
  permissions,
  org: {
    has_provider: false,
    has_number: false,
    member_count: 1,
    registration_state: "none",
  },
});

const ME = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [
    { org_id: "org-1", org_name: "Org", org_slug: "acme", role_name: "owner" },
  ],
};

const DIGEST: DeliveryDigestSettings = {
  enabled: true,
  hour: 6,
  tz: "America/New_York",
  recipients: [],
  default_recipients: ["owner@acme.com", "admin@acme.com"],
  last_sent_at: null,
};

function clientWithDigest(
  permissions: string[],
  digest: DeliveryDigestSettings = DIGEST,
  digestRoute?: RouteStub,
) {
  return makeStubClient({
    "/api/v1/me/capabilities": caps(permissions),
    "/api/v1/auth/me": ME,
    "/api/v1/analytics/delivery-digest": digestRoute ?? digest,
  });
}

describe("DeliveryDigestCard", () => {
  it("renders the saved settings", async () => {
    const client = clientWithDigest(["settings:read", "settings:write"]);

    renderWithProviders(<DeliveryDigestCard />, client);

    const checkbox = await screen.findByLabelText("Send the daily delivery email");
    expect(checkbox).toBeChecked();

    const hour = screen.getByLabelText("Send at") as HTMLSelectElement;
    expect(hour.value).toBe("6");

    const tz = screen.getByLabelText("Time zone") as HTMLSelectElement;
    expect(tz.value).toBe("America/New_York");

    const sendTo = screen.getByLabelText("Send to") as HTMLInputElement;
    expect(sendTo.placeholder).toBe("owner@acme.com, admin@acme.com");
  });

  it("renders nothing without settings:read", async () => {
    const client = clientWithDigest([]);

    renderWithProviders(<DeliveryDigestCard />, client);

    await waitFor(() => {
      expect(
        client.calls.some((call) => call.path === "/api/v1/me/capabilities"),
      ).toBe(true);
    });

    expect(screen.queryByText("Daily delivery email")).toBeNull();
    expect(
      client.calls.every((call) => !call.path.includes("/delivery-digest")),
    ).toBe(true);
  });

  it("disables the controls without settings:write", async () => {
    const client = clientWithDigest(["settings:read"]);

    renderWithProviders(<DeliveryDigestCard />, client);

    const checkbox = await screen.findByLabelText("Send the daily delivery email");
    expect(checkbox).toBeDisabled();
    expect(screen.getByLabelText("Send at")).toBeDisabled();
    expect(screen.getByLabelText("Time zone")).toBeDisabled();
    expect(screen.getByLabelText("Send to")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("saves the parsed recipients", async () => {
    const client = clientWithDigest(["settings:read", "settings:write"]);

    renderWithProviders(<DeliveryDigestCard />, client);

    const sendTo = await screen.findByLabelText("Send to");
    await userEvent.type(sendTo, "a@x.com; b@y.com ,");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(
        client.calls.find(
          (call) =>
            call.path === "/api/v1/analytics/delivery-digest" &&
            call.init?.method === "PUT",
        ),
      ).toBeDefined();
    });

    const put = client.calls.find(
      (call) =>
        call.path === "/api/v1/analytics/delivery-digest" &&
        call.init?.method === "PUT",
    )!;
    expect(put.init.json).toEqual({
      enabled: true,
      hour: 6,
      tz: "America/New_York",
      recipients: ["a@x.com", "b@y.com"],
    });
  });

  it("keeps Save disabled until something changes", async () => {
    const client = clientWithDigest(["settings:read", "settings:write"]);

    renderWithProviders(<DeliveryDigestCard />, client);

    await screen.findByLabelText("Send the daily delivery email");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

    await userEvent.click(screen.getByLabelText("Send the daily delivery email"));
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("reports a successful test send", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": caps(["settings:read", "settings:write"]),
      "/api/v1/auth/me": ME,
      "/api/v1/analytics/delivery-digest": DIGEST,
      "/api/v1/analytics/delivery-digest/test": { sent: true, day: "2024-01-15" },
    });

    renderWithProviders(<DeliveryDigestCard />, client);

    await userEvent.click(
      await screen.findByRole("button", { name: "Send me a test" }),
    );

    await screen.findByText("Test sent for 2024-01-15. Check your inbox.");
  });

  it("reports when there is nothing to send", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": caps(["settings:read", "settings:write"]),
      "/api/v1/auth/me": ME,
      "/api/v1/analytics/delivery-digest": DIGEST,
      "/api/v1/analytics/delivery-digest/test": { sent: false, day: null },
    });

    renderWithProviders(<DeliveryDigestCard />, client);

    await userEvent.click(
      await screen.findByRole("button", { name: "Send me a test" }),
    );

    await screen.findByText("Nothing to send yet: no texts in the last 7 days.");
  });

  it("shows an unknown saved time zone as an extra option", async () => {
    const client = clientWithDigest(["settings:read", "settings:write"], {
      ...DIGEST,
      tz: "Australia/Sydney",
    });

    renderWithProviders(<DeliveryDigestCard />, client);

    await screen.findByRole("option", { name: "Australia/Sydney" });
    const tz = screen.getByLabelText("Time zone") as HTMLSelectElement;
    expect(tz.value).toBe("Australia/Sydney");
  });
});
