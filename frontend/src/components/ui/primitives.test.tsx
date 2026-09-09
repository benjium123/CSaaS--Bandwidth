import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import {
  Badge,
  Button,
  Collapsible,
  Drawer,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Sheet,
  Spinner,
  TabPanel,
  Tabs,
  useCollapsible,
} from "@/components/ui/primitives";

function TabsHarnessWithPanel() {
  const [value, setValue] = React.useState("one");
  return (
    <>
      <Tabs
        id="test-tabs"
        value={value}
        onChange={(id) => setValue(id)}
        ariaLabel="Test tabs"
        tabs={[
          { id: "one", label: "One" },
          { id: "two", label: "Two" },
          { id: "three", label: "Three", disabled: true },
          { id: "four", label: "Four" },
        ]}
      />
      <TabPanel tabsId="test-tabs" id={value}>
        Panel body
      </TabPanel>
    </>
  );
}

describe("shared UI primitives", () => {
  it("keeps the original primitives rendering and forwarding className", () => {
    render(
      <>
        <Button className="test-btn" type="button">
          Save
        </Button>
        <Input aria-label="Full name" className="test-input" />
        <Badge className="test-badge">3</Badge>
        <Spinner label="Loading" />
      </>,
    );

    expect(screen.getByRole("button", { name: "Save" })).toHaveClass("test-btn");
    expect(screen.getByLabelText("Full name")).toHaveClass("test-input");
    expect(screen.getByText("3")).toHaveClass("test-badge");
    expect(screen.getByRole("status")).toHaveTextContent("Loading...");
  });

  it("Select renders a real combobox, fires onChange, and forwards disabled", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const options = (
      <>
        <option value="a">A</option>
        <option value="b">B</option>
      </>
    );

    const { rerender } = render(
      <Select aria-label="Inbox" onChange={onChange}>
        {options}
      </Select>,
    );

    const select = screen.getByRole("combobox", { name: "Inbox" });
    await user.selectOptions(select, "b");
    expect(onChange).toHaveBeenCalledTimes(1);

    rerender(
      <Select aria-label="Inbox" onChange={onChange} disabled>
        {options}
      </Select>,
    );
    expect(screen.getByRole("combobox", { name: "Inbox" })).toBeDisabled();
  });

  it("Pill renders its tone class and forwards title", () => {
    render(
      <Pill tone="success" title="Status: healthy">
        Healthy
      </Pill>,
    );

    const pill = screen.getByText("Healthy");
    expect(pill).toHaveClass("bg-emerald-500/15", "text-emerald-300");
    expect(pill).toHaveAttribute("title", "Status: healthy");
  });

  it("Section associates its heading with the region", () => {
    render(
      <Section title="Workspace" description="Manage workspace settings">
        <p>Body</p>
      </Section>,
    );

    expect(screen.getByRole("region", { name: "Workspace" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Workspace" })).toBeInTheDocument();
  });

  it("EmptyState renders title and action", () => {
    render(
      <EmptyState
        title="No numbers yet"
        description="Get started"
        action={<button type="button">Get a number</button>}
      />,
    );

    expect(screen.getByText("No numbers yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Get a number" })).toBeInTheDocument();
  });

  it("MutationStatus is idle-null, reports errors, and lets errors win over pending", () => {
    const { container, rerender } = render(<MutationStatus />);
    expect(container).toBeEmptyDOMElement();

    rerender(<MutationStatus pending />);
    expect(screen.getByRole("status")).toHaveTextContent("Saving…");

    rerender(<MutationStatus pending error={new Error("Save failed")} success="Saved" />);
    expect(screen.getByRole("alert")).toHaveTextContent("Save failed");
    expect(screen.queryByText("Saving…")).not.toBeInTheDocument();
  });

  it("Drawer hides when closed and exposes a dialog with focus and Escape close", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();

    const { rerender } = render(
      <Drawer open={false} onClose={onClose} title="Edit inbox">
        Body
      </Drawer>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    rerender(
      <Drawer open onClose={onClose} title="Edit inbox">
        <p>Body</p>
      </Drawer>,
    );

    expect(screen.getByRole("dialog", { name: "Edit inbox" })).toBeInTheDocument();
    const closeButton = screen.getByRole("button", { name: "Close" });
    expect(closeButton).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("Sheet renders a bottom-side dialog and closes on Escape", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();

    render(
      <Sheet open onClose={onClose} title="New message">
        Composer
      </Sheet>,
    );

    expect(screen.getByRole("dialog", { name: "New message" })).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("Tabs follow value, call onChange, skip disabled tabs, and wire panels", async () => {
    const user = userEvent.setup();
    render(<TabsHarnessWithPanel />);

    expect(screen.getByRole("tablist", { name: "Test tabs" })).toBeInTheDocument();

    const one = screen.getByRole("tab", { name: "One" });
    expect(one).toHaveAttribute("aria-selected", "true");
    expect(one).toHaveAttribute("aria-controls", "test-tabs-panel-one");
    expect(one).toHaveAttribute("id", "test-tabs-tab-one");
    expect(screen.getByRole("tabpanel")).toHaveAttribute("id", "test-tabs-panel-one");
    expect(screen.getByRole("tabpanel")).toHaveAttribute(
      "aria-labelledby",
      "test-tabs-tab-one",
    );

    await user.click(screen.getByRole("tab", { name: "Two" }));
    expect(screen.getByRole("tab", { name: "Two" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tabpanel")).toHaveAttribute(
      "aria-labelledby",
      "test-tabs-tab-two",
    );

    screen.getByRole("tab", { name: "Two" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Four" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Three" })).toBeDisabled();
  });

  it("Collapsible toggles, persists, and unmounts closed content", async () => {
    const user = userEvent.setup();
    const storageKey = "test.collapsible.state";
    const ui = (
      <Collapsible storageKey={storageKey} title="Advanced">
        <p>Secret content</p>
      </Collapsible>
    );

    const first = render(ui);
    expect(screen.getByText("Secret content")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Advanced/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    await user.click(screen.getByRole("button", { name: /Advanced/ }));
    expect(screen.queryByText("Secret content")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Advanced/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(localStorage.getItem(storageKey)).toBe("false");

    first.unmount();
    render(ui);

    expect(screen.queryByText("Secret content")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Advanced/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("useCollapsible survives a throwing localStorage", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });

    function Harness() {
      const [open] = useCollapsible("test.collapsible.broken");
      return (
        <button type="button" aria-expanded={open}>
          Broken
        </button>
      );
    }

    render(<Harness />);
    expect(screen.getByRole("button", { name: "Broken" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });
});
