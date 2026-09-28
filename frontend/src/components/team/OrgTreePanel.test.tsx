import * as React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  OrgTreePanel,
  buildOrgTree,
  type OrgNode,
  type OrgTreeMember,
} from "./OrgTreePanel";

function member(
  user_id: string,
  full_name: string,
  reports_to_user_id: string | null = null,
  role_name = "Agent",
): OrgTreeMember {
  return {
    user_id,
    full_name,
    email: `${user_id}@example.com`,
    role_name,
    reports_to_user_id,
  };
}

const members: OrgTreeMember[] = [
  member("hamza-id", "Hamza Khan", null, "Manager"),
  member("aisha-id", "Aisha", "hamza-id"),
  member("omar-id", "Omar", "hamza-id"),
  member("bilal-id", "Bilal"),
  member("zain-id", "Zain"),
];

/** Every user id in the tree, in render order - lets a test assert "exactly once". */
function flatten(nodes: OrgNode[]): string[] {
  const ids: string[] = [];
  const walk = (node: OrgNode): void => {
    ids.push(node.member.user_id);
    node.reports.forEach(walk);
  };
  nodes.forEach(walk);
  return ids;
}

function setup(props: Partial<React.ComponentProps<typeof OrgTreePanel>> = {}) {
  const onSetManager =
    props.onSetManager ??
    vi
      .fn<(userId: string, managerUserId: string | null) => Promise<void>>()
      .mockResolvedValue(undefined);

  render(
    <OrgTreePanel
      members={members}
      noLineUserIds={new Set<string>()}
      canEdit
      {...props}
      onSetManager={onSetManager}
    />,
  );

  return { onSetManager };
}

describe("buildOrgTree", () => {
  it("nests Hamza's two reports under Hamza", () => {
    const tree = buildOrgTree([
      member("hamza-id", "Hamza", null, "Manager"),
      member("aisha-id", "Aisha", "hamza-id"),
      member("omar-id", "Omar", "hamza-id"),
    ]);

    expect(tree).toHaveLength(1);
    expect(tree[0].member.user_id).toBe("hamza-id");
    expect(tree[0].reports.map((node) => node.member.user_id)).toEqual([
      "aisha-id",
      "omar-id",
    ]);
  });

  it("treats a member whose manager has left as a root", () => {
    const tree = buildOrgTree([member("x-id", "Xavier", "gone-id")]);

    expect(tree).toHaveLength(1);
    expect(tree[0].member.user_id).toBe("x-id");
  });

  it("terminates on a cycle and includes every member exactly once", () => {
    const tree = buildOrgTree([
      member("a-id", "Ana", "b-id"),
      member("b-id", "Ben", "a-id"),
    ]);

    const ids = flatten(tree);
    expect(ids).toHaveLength(2);
    expect([...ids].sort()).toEqual(["a-id", "b-id"]);
  });
});

describe("OrgTreePanel", () => {
  it("renders tree items with aria-level and a leads count", () => {
    setup();

    expect(screen.getByRole("treeitem", { name: "Bilal" })).toHaveAttribute(
      "aria-level",
      "1",
    );
    expect(screen.getByRole("treeitem", { name: "Aisha" })).toHaveAttribute(
      "aria-level",
      "2",
    );

    const hamza = screen.getByRole("treeitem", { name: "Hamza Khan" });
    expect(within(hamza).getByText("Leads 2")).toBeInTheDocument();
  });

  it("flags members with no line", () => {
    setup({ noLineUserIds: new Set(["zain-id"]) });

    expect(screen.getByText("No line")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Needs a spot: Zain have no line yet.",
    );
  });

  it("calls onSetManager when a manager is chosen", async () => {
    const user = userEvent.setup();
    const { onSetManager } = setup();

    await user.selectOptions(screen.getByLabelText("Manager of Zain"), "hamza-id");

    expect(onSetManager).toHaveBeenCalledWith("zain-id", "hamza-id");
  });

  it("does not offer a member's own reports as their manager", () => {
    setup();

    const select = screen.getByLabelText(
      "Manager of Hamza Khan",
    ) as HTMLSelectElement;
    const values = Array.from(select.options).map((option) => option.value);

    expect(values).not.toContain("aisha-id");
    expect(values).not.toContain("omar-id");
    expect(values).toContain("bilal-id");
    expect(values).toContain("zain-id");
  });

  it("shows the rejection message in an alert", async () => {
    const user = userEvent.setup();
    const onSetManager = vi.fn<
      (userId: string, managerUserId: string | null) => Promise<void>
    >();
    onSetManager.mockRejectedValue(new Error("That would create a loop."));

    setup({ onSetManager });

    await user.selectOptions(screen.getByLabelText("Manager of Zain"), "hamza-id");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That would create a loop.",
    );
  });

  it("renders no selects when the viewer cannot edit", () => {
    setup({ canEdit: false });

    expect(screen.queryAllByLabelText(/^Manager of /)).toHaveLength(0);
    expect(
      screen.queryByText("Drop here to make someone top level"),
    ).toBeNull();
  });
});
