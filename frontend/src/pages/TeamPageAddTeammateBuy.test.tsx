/**
 * The "buy a new number" side of the add-teammate drawer.
 *
 * Buying a line is three writes in a fixed order: create the member, order the number from
 * the carrier, then grant the freshly bought inbox to that member. If the order landed first
 * and the member create then failed, the workspace would pay every month for a number nobody
 * holds - so the order is asserted as one whole array rather than with a loose matcher.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderWithProviders } from "@/test/harness";

import {
  bodiesPostedTo,
  fillDetails,
  makeClient,
  openDrawer,
  writes,
} from "./addTeammate.fixtures";
import { TeamPage } from "./TeamPage";

afterEach(() => {
  cleanup();
});

/** Drives the drawer to the Confirm screen with a number to BUY selected. */
async function reachConfirmBuying(
  user: ReturnType<typeof userEvent.setup>,
  dialog: HTMLElement,
): Promise<void> {
  const drawer = within(dialog);
  await user.click(await drawer.findByRole("button", { name: "Buy a new number" }));
  await user.type(await drawer.findByLabelText("Area code"), "555");
  await user.click(drawer.getByRole("button", { name: "Search" }));
  await user.click(await drawer.findByLabelText("Buy (555) 019-9999"));
  await fillDetails(user, dialog);
}

describe("TeamPage add teammate, buying a number", () => {
  it("buying creates the member first, then orders, then grants", async () => {
    const user = userEvent.setup();
    const client = makeClient();
    renderWithProviders(<TeamPage />, client);
    const dialog = await openDrawer(user);

    await reachConfirmBuying(user, dialog);

    expect(
      within(dialog).getByText(
        "Ordering (555) 019-9999 will charge this workspace $15.00 a month, starting today. This is a live carrier order - it is billed until someone releases the number in Settings > Phone numbers.",
      ),
    ).toBeTruthy();

    await user.click(
      within(dialog).getByRole("button", { name: "Create teammate and buy the number" }),
    );

    await waitFor(() => {
      expect(writes(client)).toEqual([
        "/api/v1/orgs/current/members",
        "/api/v1/numbers/order",
        "/api/v1/inboxes/assignments",
      ]);
    });

    // The inbox does not exist yet on this path, so the member is created holding nothing.
    expect(bodiesPostedTo(client, "/api/v1/orgs/current/members")[0]).toEqual({
      email: "new@example.com",
      full_name: "Nikhil Roy",
      password: "hunter2hunter2",
      role_name: "agent",
      inbox_ids: [],
    });

    const put = client.calls.find(
      (call) =>
        call.init.method === "PUT" &&
        call.path.startsWith("/api/v1/inboxes/assignments"),
    );
    if (!put) throw new Error("expected a PUT to /api/v1/inboxes/assignments");
    expect(put.init.json).toEqual({
      inboxes: [{ inbox_id: "inbox-e", role: "member" }],
    });
    // The grant lands on the member just created, never on the signed-in admin.
    expect(put.path).toContain("user_id=u9");
  });

  it("a failed order says the account exists, no money moved, and what to do next", async () => {
    const user = userEvent.setup();
    const client = makeClient({ order: () => new Error("carrier rejected the order") });
    renderWithProviders(<TeamPage />, client);
    const dialog = await openDrawer(user);

    await reachConfirmBuying(user, dialog);
    await user.click(
      within(dialog).getByRole("button", { name: "Create teammate and buy the number" }),
    );

    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent).toContain("Nikhil Roy's account was created");
    expect(alert.textContent).toContain(
      "The number was NOT bought and you have not been charged",
    );
    expect(alert.textContent).toContain("Buy a number in Settings > Phone numbers");
    expect(alert.textContent).toContain("carrier rejected the order");

    // Nothing was bought, so the newly ordered inbox must never be granted to anyone.
    expect(
      client.calls.some(
        (call) =>
          call.init.method === "PUT" &&
          call.path.startsWith("/api/v1/inboxes/assignments"),
      ),
    ).toBe(false);
  });
});
