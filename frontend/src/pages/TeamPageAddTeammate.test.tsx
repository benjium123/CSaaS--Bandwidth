/**
 * The "reuse a number you already own" side of the add-teammate drawer.
 *
 * Picking an existing inbox must post that inbox_id and must NOT go near the carrier: no
 * availability search, no order. The member payload is asserted with exact equality — a
 * dropped or emptied `inbox_ids` silently creates a teammate with no line, which is exactly
 * what a loose matcher would let through.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/test/harness";

import { bodiesPostedTo, fillDetails, makeClient, openDrawer } from "./addTeammate.fixtures";
import { TeamPage } from "./TeamPage";

afterEach(() => {
  cleanup();
});

const MEMBER_POST = "/api/v1/orgs/current/members";

/** Picks the unheld inbox-c, fills the details and confirms the invite. */
async function reuseExistingNumber(
  user: ReturnType<typeof userEvent.setup>,
  dialog: HTMLElement,
): Promise<void> {
  const drawer = within(dialog);
  // The bulk assignments GET has to resolve before any number is offered.
  await user.click(await drawer.findByLabelText("Use (555) 010-0003"));
  await fillDetails(user, dialog);
  await user.click(drawer.getByRole("button", { name: "Create teammate" }));
}

describe("TeamPage add teammate", () => {
  it("reusing an existing number posts the member with that inbox, exactly", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    renderWithProviders(<TeamPage />, client);
    const dialog = await openDrawer(user);

    await reuseExistingNumber(user, dialog);

    await waitFor(() => {
      expect(bodiesPostedTo(client, MEMBER_POST)).toHaveLength(1);
    });
    expect(bodiesPostedTo(client, MEMBER_POST)[0]).toEqual({
      email: "new@example.com",
      full_name: "Nikhil Roy",
      password: "hunter2hunter2",
      role_name: "agent",
      inbox_ids: ["inbox-c"],
    });
  });

  it("a number someone already holds is never offered", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    renderWithProviders(<TeamPage />, client);
    const dialog = await openDrawer(user);
    const drawer = within(dialog);

    // Waiting on the first free number proves the bulk assignment read has landed.
    expect(await drawer.findByLabelText("Use (555) 010-0003")).toBeTruthy();
    expect(drawer.getByLabelText("Use (555) 010-0004")).toBeTruthy();

    // inbox-a is held directly; inbox-b is held only through the Billing department.
    expect(drawer.queryByLabelText("Use (555) 010-0001")).toBeNull();
    expect(drawer.queryByLabelText("Use (555) 010-0002")).toBeNull();
  });

  it("reusing a number issues no carrier call at all", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    renderWithProviders(<TeamPage />, client);
    const dialog = await openDrawer(user);

    await reuseExistingNumber(user, dialog);

    await waitFor(() => {
      expect(bodiesPostedTo(client, MEMBER_POST)).toHaveLength(1);
    });

    expect(client.calls.some((c) => c.path.startsWith("/api/v1/numbers/order"))).toBe(
      false,
    );
    expect(client.calls.some((c) => c.path.startsWith("/api/v1/numbers/available"))).toBe(
      false,
    );
  });

  it("someone without members:invite is not offered the flow", async () => {
    const client = makeClient({ permissions: ["numbers:manage", "inboxes:admin"] });
    renderWithProviders(<TeamPage />, client);
    await screen.findByText("sarah@example.com");

    expect(screen.queryByRole("button", { name: "Add teammate" })).toBeNull();
    expect(
      client.calls.some((call) => call.path.startsWith("/api/v1/numbers/available")),
    ).toBe(false);
  });
});
