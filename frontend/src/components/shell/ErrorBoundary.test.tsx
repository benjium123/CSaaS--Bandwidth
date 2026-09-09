import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ErrorBoundary } from "./ErrorBoundary";

function Bomb(): React.ReactElement {
  throw new Error("kaboom");
}

describe("ErrorBoundary", () => {
  it("renders children when nothing throws", () => {
    render(
      <ErrorBoundary>
        <p>All good</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("All good")).toBeInTheDocument();
  });

  it("catches a render error and offers a reload instead of a blank screen", async () => {
    // React logs the caught error to the console by design - silence it here so the
    // test output stays clean; it doesn't affect what we're asserting.
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const reload = vi.fn();
    const originalLocation = window.location;
    // jsdom's window.location isn't configurable in place - swap the whole object.
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...originalLocation, reload },
    });

    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    );

    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByText("kaboom")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(reload).toHaveBeenCalled();

    Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
  });
});
