/**
 * P26 verifier probes for the composer (Opus).
 *
 * Two things the phase's own tests assert only indirectly:
 *  - the Note tab is off on a VIEW-ONLY inbox (not merely on a thread-less composer)
 *  - Escape closes the "/" quick-pick and leaves focus in the message field, so the
 *    next keystroke goes on typing instead of vanishing
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Composer } from "./Composer";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const TEMPLATE = {
  id: "s1",
  name: "Greeting",
  body: "Hi there, thanks for reaching out.",
  media_asset_ids: [],
  tokens: [],
};

describe("P26 verify - composer", () => {
  it("Note tab is disabled on a view-only inbox even with a thread", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <Composer onSend={vi.fn().mockResolvedValue(undefined)} threadId="t1" disabled />,
      client,
    );

    const noteTab = screen.getByRole("tab", { name: "Note" });
    expect(noteTab).toBeDisabled();
    expect(noteTab).toHaveAttribute(
      "title",
      "Read-only inbox — you can view but not post notes",
    );
  });

  it("Escape closes the quick-pick and keeps focus in the message field", async () => {
    const client = makeStubClient({
      "/api/v1/templates": () => [TEMPLATE],
    });
    renderWithProviders(<Composer onSend={vi.fn().mockResolvedValue(undefined)} />, client);

    const field = screen.getByLabelText("Message");
    await userEvent.type(field, "/gre");
    await waitFor(() =>
      expect(
        screen.getByRole("listbox", { name: "Insert a saved reply" }),
      ).toBeInTheDocument(),
    );

    await userEvent.keyboard("{Escape}");

    expect(
      screen.queryByRole("listbox", { name: "Insert a saved reply" }),
    ).not.toBeInTheDocument();
    expect(field).toHaveFocus();

    // and typing continues in the same field
    await userEvent.keyboard("et");
    expect(field).toHaveValue("/greet");
  });

  it("clicking a quick-pick entry returns focus to the message field", async () => {
    const client = makeStubClient({
      "/api/v1/templates": () => [TEMPLATE],
    });
    renderWithProviders(<Composer onSend={vi.fn().mockResolvedValue(undefined)} />, client);

    const field = screen.getByLabelText("Message");
    await userEvent.type(field, "/gre");
    await waitFor(() =>
      expect(
        screen.getByRole("listbox", { name: "Insert a saved reply" }),
      ).toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("option", { name: /Greeting/ }));

    expect(field).toHaveValue(TEMPLATE.body);
    await waitFor(() => expect(field).toHaveFocus());
  });
});
