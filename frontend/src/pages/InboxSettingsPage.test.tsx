import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { InboxSettingsPage } from "./InboxSettingsPage";
import type { Inbox } from "@/api/conversations";

const baseInbox: Inbox = {
  id: "i1",
  name: "Support",
  color: "#000000",
  e164: "+15035551212",
  number_id: "number-1",
  my_role: "admin",
  sla_first_response_minutes: 15,
  sla_resolution_minutes: 240,
};

function renderPage(inbox: Inbox = baseInbox) {
  const client = makeStubClient({
    "/api/v1/inboxes/i1/grants": (_path: string, _init: RequestInit & { json?: unknown }) => [],
    "/api/v1/inboxes": (_path: string, init: RequestInit & { json?: unknown }) => {
      if (init.method === "PATCH") {
        return { ...inbox, ...((init.json ?? {}) as Record<string, unknown>) };
      }
      return [inbox];
    },
    "/api/v1/departments": (_path: string, _init: RequestInit & { json?: unknown }) => [],
    "/api/v1/orgs/current/members": (_path: string, _init: RequestInit & { json?: unknown }) => [],
  });

  renderWithProviders(<InboxSettingsPage />, client);
  return client;
}

describe("InboxSettingsPage reply times", () => {
  it("renders both minute fields for an admin inbox, seeded from its current values", async () => {
    renderPage();

    const first = await screen.findByRole("spinbutton", {
      name: "First reply within (minutes) for Support",
    });
    expect(first).toHaveValue(15);

    const resolve = screen.getByRole("spinbutton", {
      name: "Resolve within (minutes) for Support",
    });
    expect(resolve).toHaveValue(240);
  });

  it("renders null values as empty fields", async () => {
    renderPage({
      ...baseInbox,
      sla_first_response_minutes: null,
      sla_resolution_minutes: null,
    });

    const first = (await screen.findByRole("spinbutton", {
      name: "First reply within (minutes) for Support",
    })) as HTMLInputElement;
    expect(first.value).toBe("");

    const resolve = screen.getByRole("spinbutton", {
      name: "Resolve within (minutes) for Support",
    }) as HTMLInputElement;
    expect(resolve.value).toBe("");
  });

  it("PATCHes a new first-reply time as a number", async () => {
    const client = renderPage();

    const first = await screen.findByRole("spinbutton", {
      name: "First reply within (minutes) for Support",
    });
    await userEvent.clear(first);
    await userEvent.type(first, "20");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const patchCall = client.calls.find(
        (call) => call.path === "/api/v1/inboxes/i1" && call.init.method === "PATCH",
      );
      expect(patchCall).toBeTruthy();
      const json = patchCall?.init.json as { sla_first_response_minutes?: unknown };
      expect(json.sla_first_response_minutes).toBe(20);
    });
  });

  it("clearing a field sends the clear flag and no minute value", async () => {
    const client = renderPage();

    const first = await screen.findByRole("spinbutton", {
      name: "First reply within (minutes) for Support",
    });
    await userEvent.clear(first);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const patchCall = client.calls.find(
        (call) => call.path === "/api/v1/inboxes/i1" && call.init.method === "PATCH",
      );
      expect(patchCall).toBeTruthy();
      const json = patchCall?.init.json as Record<string, unknown>;
      expect(json.clear_sla_first_response).toBe(true);
      expect(Object.prototype.hasOwnProperty.call(json, "sla_first_response_minutes")).toBe(
        false,
      );
    });
  });

  it.each(["1.5", "0", "44641"])(
    "blocks invalid value %s, shows the sentence, and issues no PATCH",
    async (value) => {
      const client = renderPage();

      const first = await screen.findByRole("spinbutton", {
        name: "First reply within (minutes) for Support",
      });
      fireEvent.change(first, { target: { value } });
      await userEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Enter a whole number of minutes, or leave it blank.",
      );
      expect(
        client.calls.filter(
          (call) => call.path === "/api/v1/inboxes/i1" && call.init.method === "PATCH",
        ),
      ).toHaveLength(0);
    },
  );

  it("leaves 5 in the field after clearing 15 and typing 5", async () => {
    renderPage();

    const first = (await screen.findByRole("spinbutton", {
      name: "First reply within (minutes) for Support",
    })) as HTMLInputElement;
    expect(first.value).toBe("15");

    await userEvent.clear(first);
    await userEvent.type(first, "5");
    expect(first.value).toBe("5");
  });

  it("shows the reply-time labels on screen", async () => {
    renderPage();

    expect(await screen.findByText("First reply within (minutes)")).toBeInTheDocument();
    expect(screen.getByText("Resolve within (minutes)")).toBeInTheDocument();
  });

  it("still renders the existing Departments section and the inbox name field", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Departments" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Inbox name Support" })).toBeInTheDocument();
  });
});
