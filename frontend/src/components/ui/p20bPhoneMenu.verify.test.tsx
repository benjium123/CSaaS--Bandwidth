/**
 * P20b VERIFICATION probes (Opus verifier, 2026-09-10).
 *
 * PhoneNumberMenu behaviour that the unit tests shipped with P20b do not cover:
 *  - Text routes the number into the composer / new-conversation flow,
 *  - Call dials it (with the "from" number the caller passed),
 *  - Escape inside the menu closes ONLY the menu when the menu is rendered inside a
 *    Sheet - the VERDICT F4 regression, where a document-level Escape listener closed
 *    the menu AND the sheet behind it with one keypress.
 *
 * Plus a Sheet-side focus-trap probe: primitivesA11y.verify.test.tsx exercises Drawer,
 * and the mobile inbox/contact surfaces use Sheet.
 */
import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PhoneNumberMenu } from "./PhoneNumberMenu";
import { Sheet } from "./primitives";

const { dialMock } = vi.hoisted(() => ({ dialMock: vi.fn() }));

vi.mock("@/softphone/SoftphoneProvider", () => ({
  SoftphoneProvider: ({ children }: { children: React.ReactNode }) => children,
  useSoftphone: () => ({ dial: dialMock }),
}));

beforeEach(() => {
  dialMock.mockReset();
  dialMock.mockResolvedValue(undefined);
});

const E164 = "+19725550199";
const FROM = "+14694617576";

function openMenu() {
  return userEvent.click(screen.getByRole("button", { name: "Actions for (972) 555-0199" }));
}

function menu() {
  return within(screen.getByRole("menu", { name: "Phone number actions" }));
}

describe("P20b PhoneNumberMenu actions", () => {
  it("Text hands the number to the composer / new-conversation flow", async () => {
    const onText = vi.fn();
    render(<PhoneNumberMenu e164={E164} fromE164={FROM} onText={onText} />);

    await openMenu();
    await userEvent.click(menu().getByRole("menuitem", { name: "Text" }));

    expect(onText).toHaveBeenCalledTimes(1);
    expect(onText).toHaveBeenCalledWith(E164);
    expect(dialMock).not.toHaveBeenCalled();
    // The menu closes behind the action.
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("Call dials the number from the caller-supplied number", async () => {
    render(<PhoneNumberMenu e164={E164} fromE164={FROM} onText={vi.fn()} />);

    await openMenu();
    await userEvent.click(menu().getByRole("menuitem", { name: "Call" }));

    expect(dialMock).toHaveBeenCalledTimes(1);
    expect(dialMock).toHaveBeenCalledWith(E164, FROM);
  });

  it("hides the Text item entirely when no onText is supplied", async () => {
    render(<PhoneNumberMenu e164={E164} />);
    await openMenu();

    expect(menu().queryByRole("menuitem", { name: "Text" })).not.toBeInTheDocument();
    expect(menu().getByRole("menuitem", { name: "Call" })).toBeInTheDocument();
  });
});

describe("P20b PhoneNumberMenu inside a Sheet (VERDICT F4)", () => {
  function SheetHarness() {
    const [open, setOpen] = React.useState(false);
    return (
      <div>
        <button type="button" onClick={() => setOpen(true)}>
          Open sheet
        </button>
        <Sheet open={open} onClose={() => setOpen(false)} side="bottom" title="Contact">
          <PhoneNumberMenu e164={E164} fromE164={FROM} onText={vi.fn()} />
        </Sheet>
      </div>
    );
  }

  it("Escape closes only the menu, leaving the Sheet open", async () => {
    render(<SheetHarness />);
    await userEvent.click(screen.getByRole("button", { name: "Open sheet" }));
    expect(screen.getByRole("dialog", { name: "Contact" })).toBeInTheDocument();

    await openMenu();
    expect(screen.getByRole("menu", { name: "Phone number actions" })).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    // The one keypress must NOT have reached the Sheet's document-level handler.
    expect(screen.getByRole("dialog", { name: "Contact" })).toBeInTheDocument();
    // Focus goes back to the trigger, not to the void.
    expect(screen.getByRole("button", { name: "Actions for (972) 555-0199" })).toHaveFocus();
  });

  it("a second Escape, with the menu closed, does close the Sheet", async () => {
    render(<SheetHarness />);
    await userEvent.click(screen.getByRole("button", { name: "Open sheet" }));
    await openMenu();
    await userEvent.keyboard("{Escape}");
    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("P20b Sheet focus trap", () => {
  function TrapHarness() {
    const [open, setOpen] = React.useState(false);
    return (
      <div>
        <button type="button" onClick={() => setOpen(true)}>
          Choose inbox
        </button>
        <button type="button">Background button</button>
        <Sheet open={open} onClose={() => setOpen(false)} side="left" title="Inboxes">
          <button type="button">Sales</button>
        </Sheet>
      </div>
    );
  }

  it("cycles Tab inside the sheet and restores focus to the opener on Escape", async () => {
    render(<TrapHarness />);
    const opener = screen.getByRole("button", { name: "Choose inbox" });
    await userEvent.click(opener);

    const close = screen.getByRole("button", { name: "Close" });
    expect(close).toHaveFocus();

    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Sales" })).toHaveFocus();
    await userEvent.tab();
    // Wraps, rather than escaping onto "Background button" behind the modal.
    expect(close).toHaveFocus();
    expect(screen.getByRole("button", { name: "Background button" })).not.toHaveFocus();

    await userEvent.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Sales" })).toHaveFocus();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});
