import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsCallingPage } from "./SettingsCallingPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import type { Me } from "@/auth/AuthContext";
import type { CallingSettings } from "@/api/calls";

const DEFAULT_CALLING: CallingSettings = {
  recording_announcement: false,
  recording_announcement_text: null,
  announcement_text_effective: "This call may be recorded for quality and training.",
  channel_layout: "mixed",
  dispositions: [
    "Interested",
    "Not interested",
    "Callback",
    "Voicemail",
    "Wrong number",
    "No answer",
  ],
};

function renderPage({
  permissions = ["settings:read", "settings:write"],
  calling = DEFAULT_CALLING,
}: {
  permissions?: string[];
  calling?: CallingSettings;
} = {}) {
  const me: Me = {
    id: "u1",
    email: "owner@example.com",
    full_name: "Owner Person",
    memberships: [
      {
        org_id: "org-1",
        org_name: "Org",
        org_slug: "org",
        role_name: "owner",
        permissions,
      },
    ],
  };

  const client = makeStubClient({
    "/api/v1/auth/me": me,
    "/api/v1/me/capabilities": {
      permissions,
      org: {
        has_provider: false,
        has_number: false,
        member_count: 1,
        registration_state: "none",
      },
    },
    "/api/v1/orgs/current/calling": (_path: string, init: RequestInit & { json?: unknown }) => {
      if (init.method === "PATCH") {
        return { ...calling, ...(init.json as object) };
      }
      return calling;
    },
  });


  return { client, ...renderWithProviders(<SettingsCallingPage />, client) };
}

describe("SettingsCallingPage", () => {
  it("loads sections, shows the honest dual note, and never claims dual recording is active", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Recording" })).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Recording announcement" }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Call results" })).toBeInTheDocument();

    expect(
      screen.getByText(/Separate sides isn't being captured yet\./),
    ).toBeInTheDocument();
    expect(screen.queryByText(/separate sides is on|now recording separately|is active/i)).not.toBeInTheDocument();
  });

  it("saving the layout sends only channel_layout", async () => {
    const { client } = renderPage();

    await userEvent.click(
      await screen.findByRole("radio", { name: "Separate sides" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Save recording" }));

    await waitFor(() => {
      const patchCall = client.calls.find(
        (c) =>
          c.path === "/api/v1/orgs/current/calling" &&
          c.init.method === "PATCH",
      );
      expect(patchCall?.init.json).toEqual({ channel_layout: "dual" });
    });
  });

  it("announcement save sends changed fields and empty text becomes null", async () => {
    const { client } = renderPage({
      calling: {
        ...DEFAULT_CALLING,
        recording_announcement: false,
        recording_announcement_text: "Custom",
      },
    });

    await userEvent.click(
      await screen.findByRole("checkbox", {
        name: /Play an announcement before the call connects/i,
      }),
    );
    await userEvent.clear(screen.getByLabelText("Announcement"));
    await userEvent.click(screen.getByRole("button", { name: "Save announcement" }));

    await waitFor(() => {
      const patchCall = client.calls.find(
        (c) =>
          c.path === "/api/v1/orgs/current/calling" &&
          c.init.method === "PATCH",
      );
      expect(patchCall?.init.json).toEqual({
        recording_announcement: true,
        recording_announcement_text: null,
      });
    });
  });

  it("shows consent guidance with a not-legal-advice disclaimer", async () => {
    renderPage();

    expect(await screen.findByText(/not legal advice/i)).toBeInTheDocument();
    expect(
      screen.getByText(
        "California, Connecticut, Delaware, Florida, Illinois, Maryland, Massachusetts, Michigan, Montana, Nevada, New Hampshire, Oregon, Pennsylvania and Washington.",
      ),
    ).toBeInTheDocument();
  });

  it("call results: duplicate blocks save", async () => {
    renderPage();

    await userEvent.clear(await screen.findByLabelText("Call result 2"));
    await userEvent.type(screen.getByLabelText("Call result 2"), "interested");

    expect(await screen.findByText("Call results must be unique")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save call results" })).toBeDisabled();
  });

  it("call results: add + save sends the trimmed list", async () => {
    const { client } = renderPage();

    await userEvent.click(await screen.findByRole("button", { name: "Add a result" }));
    await userEvent.type(await screen.findByLabelText("Call result 7"), "  Hot lead ");
    await userEvent.click(screen.getByRole("button", { name: "Save call results" }));

    await waitFor(() => {
      const patchCall = client.calls.find(
        (c) =>
          c.path === "/api/v1/orgs/current/calling" &&
          c.init.method === "PATCH",
      );
      expect(patchCall?.init.json).toEqual({
        dispositions: [
          "Interested",
          "Not interested",
          "Callback",
          "Voicemail",
          "Wrong number",
          "No answer",
          "Hot lead",
        ],
      });
    });
  });

  it("read-only without settings:write", async () => {
    renderPage({ permissions: ["settings:read"] });

    expect(
      await screen.findByText(/only an admin can make changes/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save call results" })).toBeDisabled();
  });
});
