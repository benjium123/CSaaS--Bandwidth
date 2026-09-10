import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { ApiError } from "@/api/client";
import {
  useActivateFlow,
  useBindFlow,
  useBusinessHours,
  useCreateFlow,
  useCreateFlowVersion,
  useFlowVersions,
  useFlows,
  useNumbers,
  useQueues,
  useRingGroups,
  useAgentProfiles,
  type FlowOut,
} from "@/api/hooks";
import {
  Button,
  Card,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Spinner,
} from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/utils";

/* ---------------------------------------------------------------------------------------
 * Node draft shapes (plan DR-2). The wire shape is the flow_engine.py graph: a flat
 * `{ entry, nodes: { [id]: node } }` dict. Drafts add small editor-only conveniences
 * (menu options as an ordered list instead of a dict, empty string = "unset" for the
 * optional target selects) that get folded back into the wire shape on save.
 * ------------------------------------------------------------------------------------- */

type NodeType =
  | "menu"
  | "hours"
  | "ring_group"
  | "queue"
  | "voicemail"
  | "speak"
  | "hangup"
  | "transfer"
  // P23b: hands the call to an assistant. Terminal from the editor's point of view - the
  // assistant owns the rest of the call, exactly as "queue" hands it to a queue.
  | "assistant";

const NODE_TYPES: NodeType[] = [
  "menu",
  "hours",
  "ring_group",
  "queue",
  "voicemail",
  "speak",
  "hangup",
  "transfer",
  "assistant",
];

type MenuOptionRow = { digit: string; target: string };

type DraftNode =
  | {
      type: "menu";
      prompt: string;
      options: MenuOptionRow[];
      timeout_node: string;
      invalid_node: string;
      invalid_retries: number;
    }
  | { type: "hours"; business_hours_id: string; open: string; closed: string; holiday: string }
  | { type: "ring_group"; ring_group_id: string; no_answer: string }
  | { type: "queue"; queue_id: string }
  | { type: "voicemail"; greeting: string }
  | { type: "speak"; text: string; next: string }
  | { type: "hangup" }
  // Item 6: matches backend/app/services/flow_engine.py's "transfer" node - a required
  // `to` phone number, no outgoing edge (terminal="transferred", same shape class as
  // "queue"'s single required id field).
  | { type: "transfer"; to: string }
  // P23B_HANDOFF.md: the wire node is `{type: "assistant", profile_id}` - one required id,
  // same shape class as "queue".
  | { type: "assistant"; profile_id: string };

type NodeDraft = { id: string; node: DraftNode };

function defaultNode(type: NodeType): DraftNode {
  switch (type) {
    case "menu":
      return { type, prompt: "", options: [], timeout_node: "", invalid_node: "", invalid_retries: 2 };
    case "hours":
      return { type, business_hours_id: "", open: "", closed: "", holiday: "" };
    case "ring_group":
      return { type, ring_group_id: "", no_answer: "" };
    case "queue":
      return { type, queue_id: "" };
    case "voicemail":
      return { type, greeting: "" };
    case "speak":
      return { type, text: "", next: "" };
    case "hangup":
      return { type };
    case "transfer":
      return { type, to: "" };
    case "assistant":
      return { type, profile_id: "" };
  }
}

function fromWireDefinition(definition: unknown): { entry: string; nodes: NodeDraft[] } {
  const def = (definition ?? {}) as { entry?: unknown; nodes?: unknown };
  const entry = typeof def.entry === "string" ? def.entry : "";
  const rawNodes = (def.nodes ?? {}) as Record<string, Record<string, unknown>>;
  const nodes: NodeDraft[] = Object.entries(rawNodes).map(([id, raw]) => {
    const type = raw.type as NodeType;
    const str = (v: unknown) => (typeof v === "string" ? v : "");
    switch (type) {
      case "menu": {
        const options = raw.options && typeof raw.options === "object" ? raw.options : {};
        return {
          id,
          node: {
            type: "menu",
            prompt: str(raw.prompt),
            options: Object.entries(options as Record<string, unknown>).map(([digit, target]) => ({
              digit,
              target: str(target),
            })),
            timeout_node: str(raw.timeout_node),
            invalid_node: str(raw.invalid_node),
            invalid_retries: typeof raw.invalid_retries === "number" ? raw.invalid_retries : 2,
          },
        };
      }
      case "hours":
        return {
          id,
          node: {
            type: "hours",
            business_hours_id: str(raw.business_hours_id),
            open: str(raw.open),
            closed: str(raw.closed),
            holiday: str(raw.holiday),
          },
        };
      case "ring_group":
        return {
          id,
          node: {
            type: "ring_group",
            ring_group_id: str(raw.ring_group_id),
            no_answer: str(raw.no_answer),
          },
        };
      case "queue":
        return { id, node: { type: "queue", queue_id: str(raw.queue_id) } };
      case "voicemail":
        return { id, node: { type: "voicemail", greeting: str(raw.greeting) } };
      case "speak":
        return { id, node: { type: "speak", text: str(raw.text), next: str(raw.next) } };
      case "transfer":
        return { id, node: { type: "transfer", to: str(raw.to) } };
      case "assistant":
        return { id, node: { type: "assistant", profile_id: str(raw.profile_id) } };
      case "hangup":
      default:
        return { id, node: { type: "hangup" } };
    }
  });
  return { entry, nodes };
}

function toWireDefinition(entry: string, drafts: NodeDraft[]): { entry: string; nodes: Record<string, unknown> } {
  const nodes: Record<string, unknown> = {};
  for (const { id, node } of drafts) {
    switch (node.type) {
      case "menu": {
        const options: Record<string, string> = {};
        for (const row of node.options) {
          if (row.digit.trim() && row.target.trim()) options[row.digit.trim()] = row.target.trim();
        }
        nodes[id] = {
          type: "menu",
          prompt: node.prompt,
          options,
          ...(node.timeout_node ? { timeout_node: node.timeout_node } : {}),
          ...(node.invalid_node ? { invalid_node: node.invalid_node } : {}),
          invalid_retries: node.invalid_retries,
        };
        break;
      }
      case "hours":
        nodes[id] = {
          type: "hours",
          business_hours_id: node.business_hours_id,
          open: node.open,
          closed: node.closed,
          holiday: node.holiday,
        };
        break;
      case "ring_group":
        nodes[id] = {
          type: "ring_group",
          ring_group_id: node.ring_group_id,
          ...(node.no_answer ? { no_answer: node.no_answer } : {}),
        };
        break;
      case "queue":
        nodes[id] = { type: "queue", queue_id: node.queue_id };
        break;
      case "voicemail":
        nodes[id] = { type: "voicemail", greeting: node.greeting };
        break;
      case "speak":
        nodes[id] = {
          type: "speak",
          text: node.text,
          ...(node.next ? { next: node.next } : {}),
        };
        break;
      case "hangup":
        nodes[id] = { type: "hangup" };
        break;
      case "transfer":
        nodes[id] = { type: "transfer", to: node.to };
        break;
      case "assistant":
        nodes[id] = { type: "assistant", profile_id: node.profile_id };
        break;
    }
  }
  return { entry, nodes };
}

/** Parses ValidationFailedError's "Invalid flow definition: node 'a' ...; node 'b' ..."
 * message (services/flows.py) into per-node buckets so the editor can point at the exact
 * broken node (plan: "validation errors from the API rendered inline per node id"). An
 * error clause that doesn't name exactly one node (e.g. an infinite-loop clause naming
 * several) falls into `general`. */
function parseFlowValidationError(message: string): { byNode: Record<string, string[]>; general: string[] } {
  const body = message.replace(/^Invalid flow definition:\s*/, "");
  const byNode: Record<string, string[]> = {};
  const general: string[] = [];
  for (const clause of body.split(";").map((s) => s.trim()).filter(Boolean)) {
    const m = /^node '([^']+)'/.exec(clause);
    if (m) {
      (byNode[m[1]] ??= []).push(clause);
    } else {
      general.push(clause);
    }
  }
  return { byNode, general };
}

function flowStatusTone(status: string): "neutral" | "success" | "warning" | "danger" | "info" {
  switch (status) {
    case "active":
      return "success";
    case "draft":
      return "warning";
    case "archived":
      return "neutral";
    default:
      return "neutral";
  }
}

export function FlowsPage() {
  const { api } = useAuth();
  const { data: flows, isLoading, error, refetch } = useFlows(api);
  const [selectedName, setSelectedName] = React.useState<string | null>(null);
  const [creatingNew, setCreatingNew] = React.useState(false);

  // Latest version per flow name - `flows` is ordered (name, version desc) server-side.
  const latestByName = React.useMemo(() => {
    const map = new Map<string, FlowOut>();
    for (const f of flows ?? []) if (!map.has(f.name)) map.set(f.name, f);
    return [...map.values()];
  }, [flows]);

  return (
    <div className="grid h-full grid-cols-[minmax(280px,340px)_1fr]">
      <aside className="flex min-h-0 flex-col divide-y divide-border overflow-y-auto border-r border-border">
        <div>
          <div className="flex items-center justify-between gap-2 border-b border-border p-3">
            <h1 className="text-lg font-semibold">Flows</h1>
            <Button
              type="button"
              size="sm"
              onClick={() => {
                setSelectedName(null);
                setCreatingNew(true);
              }}
            >
              New flow
            </Button>
          </div>
          {isLoading ? (
            <Spinner label="Loading flows" />
          ) : error ? (
            <div className="space-y-2 p-4">
              <p role="alert" className="text-sm text-destructive">
                {(error as Error).message}
              </p>
              <Button type="button" size="sm" variant="outline" onClick={() => refetch()}>
                Retry
              </Button>
            </div>
          ) : latestByName.length === 0 ? (
            /* No CTA here: the one "New flow" button sits directly above this empty
               state, and a second button with the same accessible name makes every
               getByRole("button", {name: "New flow"}) ambiguous. Point at it instead. */
            <EmptyState
              className="m-4"
              title="No flows yet."
              description="Use New flow above to route incoming calls."
            />
          ) : (
            <ul aria-label="Flows">
              {latestByName.map((f) => (
                <li key={f.name}>
                  <Button
                    type="button"
                    variant="ghost"
                    aria-current={f.name === selectedName ? "true" : undefined}
                    onClick={() => {
                      setCreatingNew(false);
                      setSelectedName(f.name);
                    }}
                    className={cn(
                      "flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm font-normal hover:bg-muted",
                      f.name === selectedName && !creatingNew && "bg-muted",
                    )}
                  >
                    <span className="truncate font-medium">{f.name}</span>
                    <Pill tone={flowStatusTone(f.status)}>
                      v{f.version} {f.status}
                    </Pill>
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <BindNumberSection api={api} flows={flows ?? []} />
      </aside>

      <section className="min-h-0 overflow-y-auto p-6">
        {creatingNew ? (
          <FlowEditor
            api={api}
            key="new"
            mode="create"
            onSaved={(saved) => {
              setCreatingNew(false);
              setSelectedName(saved.name);
            }}
          />
        ) : selectedName ? (
          <FlowVersionsEditor api={api} name={selectedName} />
        ) : (
          /* Same reason as above - one "New flow" control on the page, in the header. */
          <EmptyState
            title="Select a flow"
            description="Choose a flow from the list, or use New flow to create one."
          />
        )}
      </section>
    </div>
  );
}

function FlowVersionsEditor({ api, name }: { api: import("@/api/client").ApiClient; name: string }) {
  const { data: versions, isLoading } = useFlowVersions(api, name);
  const [selectedVersionId, setSelectedVersionId] = React.useState<string | null>(null);
  const activateFlow = useActivateFlow(api);
  const [activateError, setActivateError] = React.useState<string | null>(null);

  const versionList = versions ?? [];
  const selected = versionList.find((v) => v.id === selectedVersionId) ?? versionList[0] ?? null;

  React.useEffect(() => {
    setSelectedVersionId(null);
  }, [name]);

  if (isLoading) return <Spinner label="Loading versions" />;
  if (!selected) return <EmptyState title="No versions found." />;

  async function activate() {
    setActivateError(null);
    try {
      await activateFlow.mutateAsync(selected!.id);
    } catch (err) {
      setActivateError((err as Error).message);
    }
  }

  return (
    <div className="space-y-4">
      {/* A heading plus one action row, not a Section: `Section` requires children and a
          self-closing one would render an empty, unlabelled container. */}
      <div className="flex items-start justify-between gap-4">
        <h2 className="min-w-0 truncate text-sm font-semibold">{name}</h2>
        <div className="flex shrink-0 items-center gap-2">
          <div className="flex items-center gap-2">
            <label className="text-xs text-muted-foreground" htmlFor="flow-version-select">
              Version
            </label>
            <Select
              id="flow-version-select"
              aria-label="Flow version"
              className="h-9 w-auto min-w-[130px] px-2 text-sm"
              value={selected.id}
              onChange={(e) => setSelectedVersionId(e.target.value)}
            >
              {versionList.map((v) => (
                <option key={v.id} value={v.id}>
                  v{v.version} ({v.status})
                </option>
              ))}
            </Select>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={activate}
              disabled={selected.status === "active" || activateFlow.isPending}
            >
              {selected.status === "active" ? "Active" : "Activate"}
            </Button>
          </div>
        </div>
      </div>

      <MutationStatus error={activateError} />

      <FlowEditor api={api} key={selected.id} mode="version" flowName={name} flowId={selected.id} definition={selected.definition} />
    </div>
  );
}

function FlowEditor({
  api,
  mode,
  flowId,
  flowName,
  definition,
  onSaved,
}: {
  api: import("@/api/client").ApiClient;
  mode: "create" | "version";
  flowId?: string;
  flowName?: string;
  definition?: unknown;
  onSaved?: (flow: FlowOut) => void;
}) {
  const { data: businessHours } = useBusinessHours(api);
  const { data: ringGroups } = useRingGroups(api);
  const { data: queues } = useQueues(api);
  // Same query key as the Assistants builder (["agent-profiles"]), so the picker is already
  // warm whenever the customer has just been on Settings -> AI.
  const { data: assistants } = useAgentProfiles(api);
  const createFlow = useCreateFlow(api);
  const createVersion = useCreateFlowVersion(api);

  const initial = React.useMemo(() => fromWireDefinition(definition), [definition]);
  const [name, setName] = React.useState(flowName ?? "");
  const [entry, setEntry] = React.useState(initial.entry);
  const [nodes, setNodes] = React.useState<NodeDraft[]>(initial.nodes);
  const [generalErrors, setGeneralErrors] = React.useState<string[]>([]);
  const [fieldErrors, setFieldErrors] = React.useState<Record<string, string[]>>({});
  // Item 7: which node id currently has a rejected (colliding) rename in progress, and
  // what the attempted id was - shown inline on that node's card, cleared the moment a
  // non-colliding id is typed (including reverting back to the original).
  const [renameCollision, setRenameCollision] = React.useState<{
    id: string;
    attempted: string;
  } | null>(null);
  const counterRef = React.useRef(1);

  function addNode() {
    let id = `node${counterRef.current}`;
    while (nodes.some((n) => n.id === id)) {
      counterRef.current += 1;
      id = `node${counterRef.current}`;
    }
    counterRef.current += 1;
    setNodes((prev) => [...prev, { id, node: defaultNode("speak") }]);
  }

  function removeNode(id: string) {
    setNodes((prev) => prev.filter((n) => n.id !== id));
    if (entry === id) setEntry("");
  }

  /** Item 7/6: a rename that collides with ANOTHER existing node's id, or is empty, is
   * rejected outright (not applied) rather than silently producing two nodes sharing one
   * id - the wire shape is a `{ [id]: node }` dict, so a collision would make one node
   * invisibly overwrite the other on save, and an empty id can't round-trip through it at
   * all. Called only on blur/Enter (see NodeCard's own draft state below), not per
   * keystroke - a per-keystroke commit against this same-render `nodes` list means an
   * intermediate keystroke that happens to collide with an existing id (e.g. typing
   * `x` -> `xy` -> `xyz` when a node `xy` already exists) gets silently rejected and the
   * controlled input snaps back, making it impossible to ever type past that point. */
  function renameNode(oldId: string, newId: string) {
    if (newId.trim() === "") {
      setRenameCollision({ id: oldId, attempted: newId });
      return;
    }
    const collides = newId !== oldId && nodes.some((n) => n.id === newId);
    if (collides) {
      setRenameCollision({ id: oldId, attempted: newId });
      return;
    }
    setRenameCollision((prev) => (prev?.id === oldId ? null : prev));
    setNodes((prev) => prev.map((n) => (n.id === oldId ? { ...n, id: newId } : n)));
    if (entry === oldId) setEntry(newId);
  }

  function updateNode(id: string, node: DraftNode) {
    setNodes((prev) => prev.map((n) => (n.id === id ? { ...n, node } : n)));
  }

  const nodeIds = nodes.map((n) => n.id);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setGeneralErrors([]);
    setFieldErrors({});
    const def = toWireDefinition(entry, nodes);
    try {
      if (mode === "create") {
        const saved = await createFlow.mutateAsync({ name: name.trim(), definition: def });
        onSaved?.(saved);
      } else if (flowId) {
        await createVersion.mutateAsync({ flowId, definition: def });
      }
    } catch (err) {
      if (err instanceof ApiError && err.code === "validation_failed") {
        const { byNode, general } = parseFlowValidationError(err.message);
        setFieldErrors(byNode);
        setGeneralErrors(general);
      } else {
        setGeneralErrors([(err as Error).message]);
      }
    }
  }

  const saving = createFlow.isPending || createVersion.isPending;
  const canSubmit = mode === "version" ? nodes.length > 0 : name.trim().length > 0 && nodes.length > 0;

  return (
    <Section
      title={mode === "create" ? "New flow" : "Edit flow"}
      description={mode === "create" ? "Build the initial version of the flow." : "Edit this version's definition."}
    >
      <form className="max-w-2xl space-y-4" onSubmit={save}>
        {mode === "create" && (
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor="new-flow-name">
              Flow name
            </label>
            <Input
              id="new-flow-name"
              aria-label="Flow name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </div>
        )}

        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor="flow-entry-node">
            Entry node
          </label>
          <Select
            id="flow-entry-node"
            aria-label="Entry node"
            value={entry}
            onChange={(e) => setEntry(e.target.value)}
          >
            <option value="">Select entry node…</option>
            {nodeIds.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </Select>
        </div>

        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium">Nodes</h3>
          <Button type="button" size="sm" variant="outline" onClick={addNode}>
            Add node
          </Button>
        </div>

        {generalErrors.length > 0 && (
          <ul role="alert" className="space-y-1 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
            {generalErrors.map((msg, i) => (
              <li key={i}>{msg}</li>
            ))}
          </ul>
        )}

        <div className="space-y-3">
          {nodes.length === 0 && (
            /* No CTA: the one "Add node" button is in the header directly above, and a
               duplicate with the same accessible name makes every
               getByRole("button", {name: "Add node"}) in FlowsPage.test.tsx ambiguous. */
            <EmptyState
              title="No nodes yet."
              description="Use Add node above to route the call."
            />
          )}
          {nodes.map((draft) => (
            <NodeCard
              key={draft.id}
              draft={draft}
              nodeIds={nodeIds}
              businessHours={businessHours ?? []}
              ringGroups={ringGroups ?? []}
              queues={queues ?? []}
              assistants={assistants ?? []}
              errors={[
                ...(fieldErrors[draft.id] ?? []),
                ...(renameCollision?.id === draft.id
                  ? [
                      renameCollision.attempted.trim() === ""
                        ? "Node id cannot be empty"
                        : `A node named "${renameCollision.attempted}" already exists`,
                    ]
                  : []),
              ]}
              onRename={(newId) => renameNode(draft.id, newId)}
              onChange={(node) => updateNode(draft.id, node)}
              onRemove={() => removeNode(draft.id)}
            />
          ))}
        </div>

        <div className="flex gap-2">
          <Button type="submit" disabled={!canSubmit || saving}>
            {saving ? "Saving…" : mode === "create" ? "Create flow" : "Save as new version"}
          </Button>
        </div>
      </form>
    </Section>
  );
}

function NodeCard({
  draft,
  nodeIds,
  businessHours,
  ringGroups,
  queues,
  assistants,
  errors,
  onRename,
  onChange,
  onRemove,
}: {
  draft: NodeDraft;
  nodeIds: string[];
  businessHours: { id: string; name: string }[];
  ringGroups: { id: string; name: string }[];
  queues: { id: string; name: string }[];
  assistants: { id: string; name: string }[];
  errors: string[];
  onRename: (newId: string) => void;
  onChange: (node: DraftNode) => void;
  onRemove: () => void;
}) {
  const { id, node } = draft;
  // Item 6: a per-card draft id, uncommitted while typing. The card is keyed by
  // `draft.id` in the parent's list (see FlowsPageForm), so a SUCCESSFUL rename changes
  // the key and remounts this component with `idDraft` freshly seeded from the new
  // committed id; a REJECTED rename (empty/collision) leaves this mounted with whatever
  // the user typed still in the box, so they can keep editing it rather than having it
  // snapped back to the old value mid-keystroke.
  const [idDraft, setIdDraft] = React.useState(id);

  function commitRename() {
    if (idDraft !== id) onRename(idDraft);
  }

  const targetOptions = (
    <>
      <option value="">(none)</option>
      {nodeIds.map((nid) => (
        <option key={nid} value={nid}>
          {nid}
        </option>
      ))}
    </>
  );

  return (
    <Card className="space-y-3 p-3" data-node-id={id}>
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-xs text-muted-foreground" htmlFor={`node-id-${id}`}>
          Node id
        </label>
        <Input
          id={`node-id-${id}`}
          aria-label={`Node id for ${id}`}
          className="h-8 w-40"
          value={idDraft}
          onChange={(e) => setIdDraft(e.target.value)}
          onBlur={commitRename}
          onKeyDown={(e) => {
            if (e.key !== "Enter") return;
            e.preventDefault();
            commitRename();
          }}
        />
        <label className="text-xs text-muted-foreground" htmlFor={`node-type-${id}`}>
          Type
        </label>
        <Select
          id={`node-type-${id}`}
          aria-label={`Node type for ${id}`}
          className="h-8 w-auto px-2 text-sm"
          value={node.type}
          onChange={(e) => onChange(defaultNode(e.target.value as NodeType))}
        >
          {NODE_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </Select>
        <Button type="button" size="sm" variant="destructive" className="ml-auto" onClick={onRemove}>
          Remove
        </Button>
      </div>

      {node.type === "menu" && (
        <div className="space-y-2">
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor={`menu-prompt-${id}`}>
              Prompt
            </label>
            <Input
              id={`menu-prompt-${id}`}
              aria-label={`Prompt for ${id}`}
              value={node.prompt}
              onChange={(e) => onChange({ ...node, prompt: e.target.value })}
            />
          </div>
          <div className="space-y-1">
            <span className="block text-xs text-muted-foreground">Digit options</span>
            {node.options.map((row, i) => (
              <div key={i} className="flex items-center gap-2">
                <Input
                  aria-label={`Option digit ${i + 1} for ${id}`}
                  className="h-8 w-16"
                  placeholder="digit"
                  value={row.digit}
                  onChange={(e) => {
                    const options = [...node.options];
                    options[i] = { ...options[i], digit: e.target.value };
                    onChange({ ...node, options });
                  }}
                />
                <Select
                  aria-label={`Option target ${i + 1} for ${id}`}
                  className="h-8 flex-1 px-2 text-sm"
                  value={row.target}
                  onChange={(e) => {
                    const options = [...node.options];
                    options[i] = { ...options[i], target: e.target.value };
                    onChange({ ...node, options });
                  }}
                >
                  {targetOptions}
                </Select>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove option ${i + 1} for ${id}`}
                  onClick={() => onChange({ ...node, options: node.options.filter((_, j) => j !== i) })}
                >
                  ×
                </Button>
              </div>
            ))}
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => onChange({ ...node, options: [...node.options, { digit: "", target: "" }] })}
            >
              Add option
            </Button>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor={`menu-timeout-${id}`}>
                Timeout node
              </label>
              <Select
                id={`menu-timeout-${id}`}
                aria-label={`Timeout node for ${id}`}
                className="h-8 w-full px-2 text-sm"
                value={node.timeout_node}
                onChange={(e) => onChange({ ...node, timeout_node: e.target.value })}
              >
                {targetOptions}
              </Select>
            </div>
            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor={`menu-invalid-${id}`}>
                Invalid-digit node
              </label>
              <Select
                id={`menu-invalid-${id}`}
                aria-label={`Invalid-digit node for ${id}`}
                className="h-8 w-full px-2 text-sm"
                value={node.invalid_node}
                onChange={(e) => onChange({ ...node, invalid_node: e.target.value })}
              >
                {targetOptions}
              </Select>
            </div>
          </div>
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor={`menu-retries-${id}`}>
              Invalid retries
            </label>
            <Input
              id={`menu-retries-${id}`}
              aria-label={`Invalid retries for ${id}`}
              type="number"
              min={0}
              max={5}
              className="h-8 w-24"
              value={node.invalid_retries}
              onChange={(e) => onChange({ ...node, invalid_retries: Number(e.target.value) })}
            />
          </div>
          <p className="text-[11px] text-muted-foreground">
            Speech-intent menus are not in v1 - only DTMF digit routing.
          </p>
        </div>
      )}

      {node.type === "hours" && (
        <div className="space-y-2">
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor={`hours-bh-${id}`}>
              Business hours <span aria-hidden="true">*</span>
            </label>
            <Select
              id={`hours-bh-${id}`}
              aria-label={`Business hours for ${id}`}
              aria-required="true"
              required
              className="h-8 w-full px-2 text-sm"
              value={node.business_hours_id}
              onChange={(e) => onChange({ ...node, business_hours_id: e.target.value })}
            >
              <option value="">Select…</option>
              {businessHours.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="grid grid-cols-3 gap-2">
            {(["open", "closed", "holiday"] as const).map((branch) => (
              <div key={branch} className="space-y-1">
                <label className="block text-xs capitalize text-muted-foreground" htmlFor={`hours-${branch}-${id}`}>
                  {branch} <span aria-hidden="true">*</span>
                </label>
                <Select
                  id={`hours-${branch}-${id}`}
                  aria-label={`${branch[0].toUpperCase()}${branch.slice(1)} node for ${id}`}
                  aria-required="true"
                  required
                  className="h-8 w-full px-2 text-sm"
                  value={node[branch]}
                  onChange={(e) => onChange({ ...node, [branch]: e.target.value })}
                >
                  {targetOptions}
                </Select>
              </div>
            ))}
          </div>
        </div>
      )}

      {node.type === "ring_group" && (
        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor={`rg-${id}`}>
              Ring group
            </label>
            <Select
              id={`rg-${id}`}
              aria-label={`Ring group for ${id}`}
              className="h-8 w-full px-2 text-sm"
              value={node.ring_group_id}
              onChange={(e) => onChange({ ...node, ring_group_id: e.target.value })}
            >
              <option value="">Select…</option>
              {ringGroups.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor={`rg-no-answer-${id}`}>
              No-answer node
            </label>
            <Select
              id={`rg-no-answer-${id}`}
              aria-label={`No-answer node for ${id}`}
              className="h-8 w-full px-2 text-sm"
              value={node.no_answer}
              onChange={(e) => onChange({ ...node, no_answer: e.target.value })}
            >
              {targetOptions}
            </Select>
          </div>
        </div>
      )}

      {node.type === "queue" && (
        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor={`queue-${id}`}>
            Queue
          </label>
          <Select
            id={`queue-${id}`}
            aria-label={`Queue for ${id}`}
            className="h-8 w-full px-2 text-sm"
            value={node.queue_id}
            onChange={(e) => onChange({ ...node, queue_id: e.target.value })}
          >
            <option value="">Select…</option>
            {queues.map((q) => (
              <option key={q.id} value={q.id}>
                {q.name}
              </option>
            ))}
          </Select>
        </div>
      )}

      {node.type === "assistant" && (
        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor={`assistant-${id}`}>
            Assistant <span aria-hidden="true">*</span>
          </label>
          <Select
            id={`assistant-${id}`}
            aria-label={`Assistant for ${id}`}
            className="h-8 w-full px-2 text-sm"
            value={node.profile_id}
            onChange={(e) => onChange({ ...node, profile_id: e.target.value })}
          >
            <option value="">Select…</option>
            {assistants.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </Select>
          {assistants.length === 0 && (
            <p className="text-[11px] text-muted-foreground">
              You have no assistants yet. Create one in Settings, then come back here.
            </p>
          )}
          <p className="text-[11px] text-muted-foreground">
            The assistant answers and handles the call from this point on.
          </p>
        </div>
      )}

      {node.type === "voicemail" && (
        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor={`vm-greeting-${id}`}>
            Greeting
          </label>
          <Input
            id={`vm-greeting-${id}`}
            aria-label={`Greeting for ${id}`}
            value={node.greeting}
            onChange={(e) => onChange({ ...node, greeting: e.target.value })}
          />
        </div>
      )}

      {node.type === "speak" && (
        <div className="grid grid-cols-[1fr_160px] gap-2">
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor={`speak-text-${id}`}>
              Text
            </label>
            <Input
              id={`speak-text-${id}`}
              aria-label={`Text for ${id}`}
              value={node.text}
              onChange={(e) => onChange({ ...node, text: e.target.value })}
            />
          </div>
          <div className="space-y-1">
            <label className="block text-xs text-muted-foreground" htmlFor={`speak-next-${id}`}>
              Next node
            </label>
            <Select
              id={`speak-next-${id}`}
              aria-label={`Next node for ${id}`}
              className="h-9 w-full px-2 text-sm"
              value={node.next}
              onChange={(e) => onChange({ ...node, next: e.target.value })}
            >
              {targetOptions}
            </Select>
          </div>
        </div>
      )}

      {node.type === "transfer" && (
        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor={`transfer-to-${id}`}>
            Transfer to <span aria-hidden="true">*</span>
          </label>
          <Input
            id={`transfer-to-${id}`}
            aria-label={`Transfer to for ${id}`}
            aria-required="true"
            required
            placeholder="+19725550199"
            value={node.to}
            onChange={(e) => onChange({ ...node, to: e.target.value })}
          />
          <p className="text-[11px] text-muted-foreground">
            Ends the call flow by transferring to this number - it has no outgoing node.
          </p>
        </div>
      )}

      {node.type === "hangup" && <p className="text-xs text-muted-foreground">Ends the call.</p>}

      {errors.length > 0 && (
        <ul role="alert" className="space-y-1 text-xs text-destructive">
          {errors.map((msg, i) => (
            <li key={i}>{msg}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function BindNumberSection({
  api,
  flows,
}: {
  api: import("@/api/client").ApiClient;
  flows: FlowOut[];
}) {
  const { data: numbers } = useNumbers(api);
  const bindFlow = useBindFlow(api);
  const [numberId, setNumberId] = React.useState("");
  const [flowId, setFlowId] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [ok, setOk] = React.useState(false);

  const activeFlows = flows.filter((f) => f.status === "active");

  async function bind(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setOk(false);
    try {
      await bindFlow.mutateAsync({ numberId, flowId: flowId || null });
      setOk(true);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <Section title="Bind number to flow" className="p-3">
      <form className="space-y-2" onSubmit={bind}>
        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor="bind-number">
            Number
          </label>
          <Select
            id="bind-number"
            aria-label="Number to bind"
            value={numberId}
            onChange={(e) => setNumberId(e.target.value)}
            required
          >
            <option value="">Select a number…</option>
            {(numbers ?? []).map((n) => (
              <option key={n.id} value={n.id}>
                {formatPhone(n.e164)}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-1">
          <label className="block text-xs text-muted-foreground" htmlFor="bind-flow">
            Flow (active version)
          </label>
          <Select
            id="bind-flow"
            aria-label="Flow to bind"
            value={flowId}
            onChange={(e) => setFlowId(e.target.value)}
          >
            <option value="">None (default behavior)</option>
            {activeFlows.map((f) => (
              <option key={f.id} value={f.id}>
                {f.name} (v{f.version})
              </option>
            ))}
          </Select>
        </div>
        <MutationStatus
          error={error}
          pending={bindFlow.isPending}
          success={ok ? "Bound." : null}
          pendingLabel="Binding…"
        />
        <Button type="submit" size="sm" disabled={!numberId || bindFlow.isPending}>
          Bind
        </Button>
      </form>
    </Section>
  );
}
