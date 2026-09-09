/**
 * P20a accessibility probes (Opus verifier, 2026-09-10).
 *
 * CHARACTERISATION tests, same rule as navGating.verify.test.tsx: they pin today's
 * behaviour, and the "CORRECT" comment says what to assert once the gap is closed.
 *
 * GAP A1 - CLOSED in P20b. `Overlay` in primitives.tsx now traps Tab/Shift+Tab inside the
 *   aria-modal dialog and restores focus to the element that opened it on close. The two
 *   tests below were characterisation tests pinning the OLD (wrong) behaviour; they now
 *   assert the correct behaviour, which is what P20b's mobile inbox sheet and contact
 *   sheet depend on. Do not weaken them back.
 */
import * as React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Drawer, Tabs, TabPanel, panelId, tabId } from "./primitives";

function DrawerHarness() {
  const [open, setOpen] = React.useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Open drawer
      </button>
      <button type="button">Background button</button>
      <Drawer open={open} onClose={() => setOpen(false)} title="Assign owner">
        <button type="button">Inside drawer</button>
      </Drawer>
    </div>
  );
}

describe("Drawer accessibility", () => {
  it("labels the dialog and moves focus to Close on open", async () => {
    render(<DrawerHarness />);
    await userEvent.click(screen.getByRole("button", { name: "Open drawer" }));

    const dialog = screen.getByRole("dialog", { name: "Assign owner" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
  });

  it("traps Tab inside the dialog", async () => {
    render(<DrawerHarness />);
    await userEvent.click(screen.getByRole("button", { name: "Open drawer" }));
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();

    await userEvent.tab(); // Close -> Inside drawer (the last focusable in the panel)
    expect(screen.getByRole("button", { name: "Inside drawer" })).toHaveFocus();
    await userEvent.tab(); // ...and wraps back to Close instead of escaping the overlay.
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
    expect(screen.getByRole("dialog", { name: "Assign owner" })).toContainElement(
      document.activeElement as HTMLElement,
    );
  });

  it("traps Shift+Tab inside the dialog", async () => {
    render(<DrawerHarness />);
    await userEvent.click(screen.getByRole("button", { name: "Open drawer" }));
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();

    await userEvent.tab({ shift: true }); // from the first focusable, wrap to the last
    expect(screen.getByRole("button", { name: "Inside drawer" })).toHaveFocus();
  });

  it("restores focus to the element that opened it on close", async () => {
    render(<DrawerHarness />);
    const opener = screen.getByRole("button", { name: "Open drawer" });
    await userEvent.click(opener);
    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});

describe("Tabs roles", () => {
  it("wires tablist/tab/tabpanel with both aria directions", async () => {
    function Harness() {
      const [value, setValue] = React.useState("one");
      return (
        <div>
          <Tabs
            id="probe"
            ariaLabel="Probe tabs"
            value={value}
            onChange={setValue}
            tabs={[
              { id: "one", label: "One" },
              { id: "two", label: "Two" },
            ]}
          />
          <TabPanel tabsId="probe" id={value}>
            Panel {value}
          </TabPanel>
        </div>
      );
    }
    render(<Harness />);

    const tablist = screen.getByRole("tablist", { name: "Probe tabs" });
    expect(tablist).toBeInTheDocument();
    const one = screen.getByRole("tab", { name: "One" });
    expect(one).toHaveAttribute("aria-selected", "true");
    expect(one).toHaveAttribute("aria-controls", panelId("probe", "one"));
    expect(one).toHaveAttribute("id", tabId("probe", "one"));

    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("aria-labelledby", tabId("probe", "one"));

    await userEvent.click(screen.getByRole("tab", { name: "Two" }));
    expect(screen.getByRole("tab", { name: "Two" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Panel two");
  });
});
