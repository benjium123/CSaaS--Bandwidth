/**
 * Org chart for the Team page.
 *
 * `buildOrgTree` folds the flat member list (each row carries `reports_to_user_id`) into a
 * tree of `OrgNode`s. It is pure and exported so the shape can be unit-tested without React,
 * and it is defensive about the data on purpose: a manager who has left the workspace, a row
 * pointing at itself, or a manager loop created outside this UI must not spin the renderer.
 *
 * The panel offers TWO ways to change who manages whom, because drag and drop alone would
 * lock out keyboard and screen-reader users: native HTML5 DnD on the row, and a `<select>`
 * per row that lists everyone except the person themselves and their own reports (dropping
 * onto one of those would make them their own manager). The select is the one the tests use.
 *
 * Nothing here calls the network directly - `onSetManager` is supplied by the page, and the
 * panel only tracks which row is saving and the last error message.
 */
import * as React from "react";

import { InitialsAvatar } from "@/components/ui/consoleChrome";
import { Pill } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export type OrgTreeMember = {
  user_id: string;
  full_name: string;
  email: string;
  role_name: string;
  reports_to_user_id?: string | null;
};

export type OrgNode = { member: OrgTreeMember; reports: OrgNode[] };

function byFullName(a: OrgTreeMember, b: OrgTreeMember): number {
  return a.full_name.localeCompare(b.full_name);
}

export function buildOrgTree(members: OrgTreeMember[]): OrgNode[] {
  const byId = new Map<string, OrgTreeMember>();
  for (const member of members) byId.set(member.user_id, member);

  const childrenOf = new Map<string, OrgTreeMember[]>();
  const rootMembers: OrgTreeMember[] = [];

  for (const member of members) {
    const managerId = member.reports_to_user_id;
    // A root is anybody nobody manages: no manager set, a manager who is no longer in the
    // list, or a row pointing at itself (which we must not follow, or the walk below would
    // never end).
    if (managerId != null && managerId !== member.user_id && byId.has(managerId)) {
      const siblings = childrenOf.get(managerId);
      if (siblings) siblings.push(member);
      else childrenOf.set(managerId, [member]);
    } else {
      rootMembers.push(member);
    }
  }

  // One shared set for the whole build, so a member is rendered exactly once even when the
  // data contains a cycle or two managers point at the same report.
  const placed = new Set<string>();

  const buildNode = (member: OrgTreeMember): OrgNode => {
    placed.add(member.user_id);
    const reports: OrgNode[] = [];
    for (const child of (childrenOf.get(member.user_id) ?? []).slice().sort(byFullName)) {
      // Checked right before descending: an earlier sibling may already have placed this
      // child further down its own subtree.
      if (placed.has(child.user_id)) continue;
      reports.push(buildNode(child));
    }
    return { member, reports };
  };

  const nodes = rootMembers.slice().sort(byFullName).map(buildNode);

  // Anything still unplaced is trapped in a loop no root reaches. Promote each one to its
  // own root so it still shows up - once - without the recursion above spinning.
  for (const member of members.slice().sort(byFullName)) {
    if (!placed.has(member.user_id)) nodes.push(buildNode(member));
  }

  return nodes;
}

/** user_id -> every id sitting below it in the built tree, used to forbid loops client-side. */
function buildDescendantIndex(nodes: OrgNode[]): Map<string, Set<string>> {
  const index = new Map<string, Set<string>>();
  const visit = (node: OrgNode): Set<string> => {
    const descendants = new Set<string>();
    for (const report of node.reports) {
      descendants.add(report.member.user_id);
      for (const id of visit(report)) descendants.add(id);
    }
    index.set(node.member.user_id, descendants);
    return descendants;
  };
  for (const node of nodes) visit(node);
  return index;
}

const WOULD_LOOP = "That would make someone their own manager.";

export function OrgTreePanel({
  members,
  noLineUserIds,
  canEdit,
  onSetManager,
}: {
  members: OrgTreeMember[];
  /** user ids that hold NO line at all (no direct grant, no department). Flag them. */
  noLineUserIds: Set<string>;
  canEdit: boolean;
  /** Persist a new manager (null = top level). Rejects with an Error whose message is shown. */
  onSetManager: (userId: string, managerUserId: string | null) => Promise<void>;
}): JSX.Element {
  const tree = React.useMemo(() => buildOrgTree(members), [members]);
  const descendantIndex = React.useMemo(() => buildDescendantIndex(tree), [tree]);

  const membersByName = React.useMemo(
    () => members.slice().sort(byFullName),
    [members],
  );

  const noLineMembers = React.useMemo(
    () => membersByName.filter((member) => noLineUserIds.has(member.user_id)),
    [membersByName, noLineUserIds],
  );

  const [draggingId, setDraggingId] = React.useState<string | null>(null);
  const [pendingId, setPendingId] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const save = React.useCallback(
    async (userId: string, managerUserId: string | null) => {
      setError(null);
      setPendingId(userId);
      try {
        await onSetManager(userId, managerUserId);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setPendingId(null);
      }
    },
    [onSetManager],
  );

  const handleDrop = (targetId: string) => {
    const sourceId = draggingId;
    setDraggingId(null);
    if (sourceId == null) return;
    const wouldLoop =
      sourceId === targetId ||
      (descendantIndex.get(sourceId)?.has(targetId) ?? false);
    if (wouldLoop) {
      setError(WOULD_LOOP);
      return;
    }
    void save(sourceId, targetId);
  };

  const handleTopLevelDrop = () => {
    const sourceId = draggingId;
    setDraggingId(null);
    if (sourceId == null) return;
    void save(sourceId, null);
  };

  const renderNode = (node: OrgNode, depth: number): React.ReactNode => {
    const member = node.member;
    const reportCount = node.reports.length;
    const excluded = descendantIndex.get(member.user_id) ?? new Set<string>();
    const managerOptions = membersByName.filter(
      (other) => other.user_id !== member.user_id && !excluded.has(other.user_id),
    );

    return (
      <div
        key={member.user_id}
        role="treeitem"
        aria-level={depth + 1}
        aria-label={member.full_name}
      >
        <div
          draggable={canEdit}
          onDragStart={
            canEdit
              ? (event: React.DragEvent<HTMLDivElement>) => {
                  setDraggingId(member.user_id);
                  if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
                }
              : undefined
          }
          onDragOver={canEdit ? (event) => event.preventDefault() : undefined}
          onDrop={
            canEdit
              ? (event) => {
                  event.preventDefault();
                  handleDrop(member.user_id);
                }
              : undefined
          }
          style={{ paddingLeft: depth * 20 }}
          className={cn(
            "flex flex-wrap items-center gap-3 rounded-md px-3 py-2 hover:bg-muted/50",
            canEdit && "cursor-grab",
          )}
        >
          <InitialsAvatar name={member.full_name} seed={member.user_id} size="row" />
          <span className="font-semibold">{member.full_name}</span>
          <Pill tone="info">{member.role_name}</Pill>
          {reportCount > 0 ? (
            <span className="text-sm text-muted-foreground">
              {`Leads ${reportCount}`}
            </span>
          ) : null}
          {noLineUserIds.has(member.user_id) ? (
            <Pill tone="warning">No line</Pill>
          ) : null}
          {canEdit ? (
            <select
              aria-label={`Manager of ${member.full_name}`}
              className="h-8 rounded-[var(--cx-r-xs,10px)] border border-border bg-background px-2 text-sm text-muted-foreground focus-visible:outline-none focus-visible:ring-2"
              value={member.reports_to_user_id ?? ""}
              disabled={pendingId === member.user_id}
              onChange={(event) => {
                const value = event.target.value;
                void save(member.user_id, value === "" ? null : value);
              }}
            >
              <option value="">No manager</option>
              {managerOptions.map((option) => (
                <option key={option.user_id} value={option.user_id}>
                  {option.full_name}
                </option>
              ))}
            </select>
          ) : null}
        </div>
        {reportCount > 0 ? (
          <div role="group">
            {node.reports.map((report) => renderNode(report, depth + 1))}
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <div className="flex flex-col gap-3">
      <div>
        <h2 className="text-[15px] font-semibold tracking-[-0.01em]">Org chart</h2>
        <p className="text-sm text-muted-foreground">
          Managers can read, reply and call on every line of the people below them.
        </p>
      </div>

      {noLineMembers.length > 0 ? (
        <div
          role="status"
          className="rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground"
        >
          {`Needs a spot: ${noLineMembers
            .map((member) => member.full_name)
            .join(", ")} have no line yet.`}
        </div>
      ) : null}

      {canEdit ? (
        <div
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            handleTopLevelDrop();
          }}
          className="rounded-md border border-dashed border-border px-3 py-2 text-sm text-muted-foreground"
        >
          Drop here to make someone top level
        </div>
      ) : null}

      <div role="tree" aria-label="Org chart" className="flex flex-col gap-1">
        {tree.map((node) => renderNode(node, 0))}
      </div>

      {error != null ? (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}
