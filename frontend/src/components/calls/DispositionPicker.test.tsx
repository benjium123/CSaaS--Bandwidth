import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DispositionPicker } from "./DispositionPicker";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import type { Me } from "@/auth/AuthContext";

const ME: Me = {
  id: "u1",
  email: "owner@example.com",
  full_name: "Owner Person",
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org",
      org_slug: "org",
      role_name: "owner",
      permissions: ["calls:read", "settings:read"],
    },
  ],
};

const ORG = {
  id: "org-1",
  name: "Org Name",
  slug: "org",
};

function makeClient({
  permissions = ["calls:read", "settings:read"],
  inboxes = [],
  catalog = ["Hot lead", "Not now"],
  dispositionPatch,
}: {
  permissions?: string[];
  inboxes?: unknown[];
  catalog?: string[];
  dispositionPatch?: unknown;
} = {}) {
  const calling = {
    recording_announcement: false,
    recording_announcement_text: null,
    announcement_text_effective: "This call may be recorded for quality and training.",
    channel_layout: "mixed",
    dispositions: catalog,
  };

  const client = makeStubClient({
    "/api/v1/auth/me": {
      ...ME,
      memberships: [
        {
          ...ME.memberships[0],
          permissions,
        },
      ],
    },
    "/api/v1/me/capabilities": {
      permissions,
      org: { has_provider: false, has_number: false, member_count: 1, registration_state: "none" },
    },
    "/api/v1/orgs/current/calling": calling,
    "/api/v1/calls/dispositions": { dispositions: catalog },
    "/api/v1/inboxes": inboxes,
    "/api/v1/calls/call-1/disposition":
      dispositionPatch !== undefined
        ? dispositionPatch
        : (_path: string, init: RequestInit & { json?: unknown }) => {
            if (init.method === "PATCH") return { id: "call-1" };
            return {};
          },
  });

  void ORG;

  return client;
}

describe("DispositionPicker", () => {
  it("lists only the workspace's configured results", async () => {
    const client = makeClient();
    renderWithProviders(
      <DispositionPicker
        api={client}
        callId="call-1"
        ourE164="+12145550100"
        disposition={null}
        note={null}
      />,
      client,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Add call result" }));
    const select = await screen.findByRole("combobox", { name: "Call result" });
    const options = within(select).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "No result",
      "Hot lead",
      "Not now",
    ]);
  });

  it("saves the result and note", async () => {
    const client = makeClient();
    renderWithProviders(
      <DispositionPicker
        api={client}
        callId="call-1"
        ourE164="+12145550100"
        disposition={null}
        note={null}
      />,
      client,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Add call result" }));
    await userEvent.selectOptions(
      await screen.findByRole("combobox", { name: "Call result" }),
      "Hot lead",
    );
    await userEvent.type(screen.getByLabelText("Note"), " call Tue ");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const patchCall = client.calls.find(
        (c) =>
          c.path === "/api/v1/calls/call-1/disposition" &&
          c.init.method === "PATCH",
      );
      expect(patchCall?.init.json).toEqual({ disposition: "Hot lead", note: "call Tue" });
    });
  });

  it("keeps the note disabled until a result is picked", async () => {
    const client = makeClient();
    renderWithProviders(
      <DispositionPicker
        api={client}
        callId="call-1"
        ourE164="+12145550100"
        disposition={null}
        note={null}
      />,
      client,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Add call result" }));
    const note = screen.getByLabelText("Note");
    expect(note).toBeDisabled();

    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Call result" }), "Hot lead");
    expect(note).toBeEnabled();
  });

  it("clearing the result sends nulls", async () => {
    const client = makeClient();
    renderWithProviders(
      <DispositionPicker
        api={client}
        callId="call-1"
        ourE164="+12145550100"
        disposition="Not now"
        note="x"
      />,
      client,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Change" }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Call result" }),
      "No result",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const patchCall = client.calls.find(
        (c) =>
          c.path === "/api/v1/calls/call-1/disposition" &&
          c.init.method === "PATCH",
      );
      expect(patchCall?.init.json).toEqual({ disposition: null, note: null });
    });
  });

  it("lets an agent with calls:read but no settings:read pick from the call-result list", async () => {
    const client = makeClient({ permissions: ["calls:read"] });
    renderWithProviders(
      <DispositionPicker
        api={client}
        callId="call-1"
        ourE164="+12145550100"
        disposition="Not now"
        note="x"
      />,
      client,
    );

    expect(await screen.findByRole("button", { name: "Change" })).toBeInTheDocument();
    expect(client.calls.some((c) => c.path === "/api/v1/calls/dispositions")).toBe(true);
    expect(client.calls.some((c) => c.path.startsWith("/api/v1/orgs/current/calling"))).toBe(false);
  });

  it("shows the saved result read-only without calls:read and never loads the list", async () => {
    const client = makeClient({ permissions: [] });
    renderWithProviders(
      <DispositionPicker
        api={client}
        callId="call-1"
        ourE164="+12145550100"
        disposition="Not now"
        note="x"
      />,
      client,
    );

    await screen.findByText("Not now");
    expect(client.calls.some((c) => c.path === "/api/v1/calls/dispositions")).toBe(false);
    expect(screen.queryByRole("button", { name: "Change" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add call result" })).not.toBeInTheDocument();
  });

  it("offers nothing on a read-only inbox", async () => {
    const client = makeClient({
      inboxes: [
        {
          id: "i1",
          name: "Sales",
          color: "#22c55e",
          e164: "+12145550100",
          number_id: "n1",
          my_role: "viewer",
        },
      ],
    });

    renderWithProviders(
      <DispositionPicker
        api={client}
        callId="call-1"
        ourE164="+12145550100"
        disposition={null}
        note={null}
      />,
      client,
    );

    await waitFor(() => {
      expect(client.calls.some((c) => c.path === "/api/v1/inboxes")).toBe(true);
      expect(client.calls.some((c) => c.path === "/api/v1/calls/dispositions")).toBe(true);
    });

    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "Add call result" }),
      ).not.toBeInTheDocument();
    });
  });

  it("surfaces the server's sentence", async () => {
    const client = makeClient({
      dispositionPatch: new Error("That call result is not on this workspace's list"),
    });

    renderWithProviders(
      <DispositionPicker
        api={client}
        callId="call-1"
        ourE164="+12145550100"
        disposition={null}
        note={null}
      />,
      client,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Add call result" }));
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Call result" }),
      "Hot lead",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("That call result is not on this workspace's list"),
    ).toBeInTheDocument();
  });
});
