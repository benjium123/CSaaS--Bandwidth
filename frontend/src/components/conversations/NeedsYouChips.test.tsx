import * as React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { NeedsYouChips } from "./NeedsYouChips";

function renderChips(overrides: Partial<React.ComponentProps<typeof NeedsYouChips>> = {}) {
  return render(<NeedsYouChips counts={{}} active={null} onPick={vi.fn()} {...overrides} />);
}

describe("NeedsYouChips", () => {
  it("renders only the chips that have something behind them", () => {
    renderChips({ counts: { unread: { n: 3, more: false }, unresponded: { n: 0, more: false } } });

    expect(screen.getByRole("group", { name: "Needs you" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Unread: 3" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Waiting: 0" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Overdue/ })).toBeNull();
  });

  it("keeps the declared order", () => {
    renderChips({
      counts: {
        unread: { n: 1, more: false },
        unresponded: { n: 2, more: false },
        overdue: { n: 3, more: false },
        missed: { n: 4, more: false },
        voicemail: { n: 5, more: false },
        assigned: { n: 6, more: false },
      },
    });

    // Most urgent first (missed calls), overdue last.
    const names = screen.getAllByRole("button").map((button) => button.getAttribute("aria-label"));
    expect(names).toEqual(["Missed: 4", "Waiting: 2", "Voicemails: 5", "Assigned: 6", "Unread: 1", "Overdue: 3"]);
  });

  it("shows a trailing + when the count is capped", () => {
    renderChips({ counts: { unread: { n: 50, more: true } } });

    expect(screen.getByRole("button", { name: "Unread: 50+" })).toBeTruthy();
    expect(screen.getByText("50+")).toBeTruthy();
  });

  it("marks the active chip and clears the filter when it is clicked again", async () => {
    const user = userEvent.setup();
    const onPick = vi.fn();
    renderChips({ counts: { unread: { n: 2, more: false } }, active: "unread", onPick });

    const chip = screen.getByRole("button", { name: "Unread: 2" });
    expect(chip.getAttribute("aria-pressed")).toBe("true");

    await user.click(chip);

    expect(onPick).toHaveBeenCalledWith(null);
  });

  it("picks an inactive chip", async () => {
    const user = userEvent.setup();
    const onPick = vi.fn();
    renderChips({ counts: { overdue: { n: 4, more: false } }, onPick });

    const chip = screen.getByRole("button", { name: "Overdue: 4" });
    expect(chip.getAttribute("aria-pressed")).toBe("false");

    await user.click(chip);

    expect(onPick).toHaveBeenCalledWith("overdue");
  });

  it("paints the overdue count as destructive", () => {
    renderChips({ counts: { overdue: { n: 4, more: false } } });

    expect(screen.getByText("4").className).toContain("text-destructive");
  });

  it("renders nothing when every count is zero or missing", () => {
    const { container } = renderChips({
      counts: { unread: { n: 0, more: false }, overdue: { n: 0, more: true } },
    });

    expect(container.childNodes).toHaveLength(0);
  });

  it("renders nothing when there are no counts at all", () => {
    const { container } = renderChips();

    expect(container.childNodes).toHaveLength(0);
  });
});
