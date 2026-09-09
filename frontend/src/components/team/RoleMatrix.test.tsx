import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RoleMatrix } from "./RoleMatrix";

describe("RoleMatrix", () => {
  it("renders grouped legends and checks the boxes in value", () => {
    render(
      <RoleMatrix
        value={["contacts:read"]}
        onChange={() => {}}
        grantablePermissions={() => true}
      />,
    );

    expect(screen.getByText("Contacts")).toBeInTheDocument();
    expect(screen.getByText("Inbox")).toBeInTheDocument();
    expect(screen.getByLabelText("Can see contacts")).toBeChecked();
  });

  it("disables a permission the user cannot grant and carries the tooltip", () => {
    render(
      <RoleMatrix
        value={[]}
        onChange={() => {}}
        grantablePermissions={(key) => key !== "contacts:assign"}
      />,
    );

    const checkbox = screen.getByLabelText("Can reassign contacts");
    expect(checkbox).toBeDisabled();
    expect(checkbox).toHaveAttribute(
      "title",
      "You don't have this permission yourself, so you can't grant it.",
    );
  });

  it("keeps a non-grantable permission checked and disabled when it is already in value", () => {
    render(
      <RoleMatrix
        value={["contacts:assign"]}
        onChange={() => {}}
        grantablePermissions={(key) => key !== "contacts:assign"}
      />,
    );

    const checkbox = screen.getByLabelText("Can reassign contacts");
    expect(checkbox).toBeChecked();
    expect(checkbox).toBeDisabled();
  });

  it("adds and removes permissions without mutating the previous array", async () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <RoleMatrix value={[]} onChange={onChange} grantablePermissions={() => true} />,
    );

    await userEvent.click(screen.getByLabelText("Can see contacts"));
    expect(onChange).toHaveBeenCalledWith(["contacts:read"]);

    const previous = ["contacts:read"];
    rerender(
      <RoleMatrix value={previous} onChange={onChange} grantablePermissions={() => true} />,
    );

    await userEvent.click(screen.getByLabelText("Can see contacts"));
    expect(onChange).toHaveBeenLastCalledWith([]);
    expect(previous).toEqual(["contacts:read"]);
  });

  it("disables every checkbox and shows the read-only note", () => {
    render(
      <RoleMatrix
        value={["contacts:read"]}
        onChange={() => {}}
        readOnly
        grantablePermissions={() => true}
      />,
    );

    expect(screen.getByText("Built-in roles can't be edited.")).toBeInTheDocument();
    expect(screen.getByLabelText("Can see contacts")).toBeDisabled();
  });
});
