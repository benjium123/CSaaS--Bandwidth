/**
 * P20a accessibility probes (Opus verifier, 2026-09-10).
 *
 * CHARACTERISATION tests, same rule as navGating.verify.test.tsx: they pin today's
 * behaviour, and the "CORRECT" comment says what to assert once the gap is closed.
 *
 * GAP A1 - Drawer/Sheet do NOT trap focus and do NOT restore it.
 *   primitives.tsx:288-356 (`Overlay`) focuses the Close button on open and closes on
 *   Escape or backdrop click, but there is no Tab wrap and nothing refocuses the
 *   element that opened it. Non-blocking today only because nothing in the app imports
 *   the new Drawer/Sheet yet (the existing AssignOwnerDrawer / RatesDrawer /
 *   DeliveriesDrawer are hand-rolled). P20b puts real content behind them - mobile
 *   sheets, the contact panel - so it must be closed before then.
 *   CORRECT: Tab from the last focusable wraps to the first, Shift+Tab from the first
 *   wraps to the last, and closing returns focus to the opener.
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

  it("GAP A1: focus is not trapped inside the dialog", async () => {
    render(<DrawerHarness />);
    await userEvent.click(screen.getByRole("button", { name: "Open drawer" }));
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();

    await userEvent.tab(); // Close -> Inside drawer
    expect(screen.getByRole("button", { name: "Inside drawer" })).toHaveFocus();
    await userEvent.tab(); // ...and straight back out to the page behind the overlay.
    // CORRECT once A1 is fixed: focus wraps to the Close button instead.
    expect(screen.getByRole("button", { name: "Inside drawer" })).not.toHaveFocus();
    expect(screen.getByRole("dialog", { name: "Assign owner" })).not.toContainElement(
      document.activeElement as HTMLElement,
    );
  });

  it("GAP A1: closing does not restore focus to the element that opened it", async () => {
    render(<DrawerHarness />);
    const opener = screen.getByRole("button", { name: "Open drawer" });
    await userEvent.click(opener);
    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // CORRECT once A1 is fixed: expect(opener).toHaveFocus();
    expect(opener).not.toHaveFocus();
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
