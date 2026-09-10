import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FlowsPage } from "./FlowsPage";
import { ApiError } from "@/api/client";
import { makeStubClient, renderWithProviders } from "@/test/harness";

/** Polls the stub client's recorded calls until one matching `path`+`method` shows up. */
async function waitForCall(
  client: ReturnType<typeof makeStubClient>,
  path: string,
  method: string,
): Promise<{ path: string; init: RequestInit & { json?: unknown } }> {
  await waitFor(() =>
    expect(client.calls.some((c) => c.path === path && c.init.method === method)).toBe(true),
  );
  return client.calls.find((c) => c.path === path && c.init.method === method)!;
}

const EMPTY_LISTS = {
  "/api/v1/agent/profiles": [],
  "/api/v1/business-hours": [],
  "/api/v1/ring-groups": [],
  "/api/v1/queues": [],
  "/api/v1/numbers": [],
};

const CREATED_FLOW = {
  id: "flow-1",
  name: "Sales IVR",
  version: 1,
  status: "draft",
  definition: { entry: "node1", nodes: {} },
  created_at: new Date().toISOString(),
};

const FLOW_V1 = {
  id: "flow-1",
  name: "Sales",
  version: 1,
  status: "draft",
  definition: { entry: "", nodes: {} },
  created_at: new Date().toISOString(),
};

describe("FlowsPage", () => {
  it("creates a flow with a menu node", async () => {
    const client = makeStubClient({
      "/api/v1/flows": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return CREATED_FLOW;
        return [];
      },
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.type(screen.getByLabelText("Flow name"), "Sales IVR");

    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.selectOptions(screen.getByLabelText("Node type for node1"), "menu");
    await userEvent.type(screen.getByLabelText("Prompt for node1"), "Press 1 for sales");
    await userEvent.selectOptions(screen.getByLabelText("Entry node"), "node1");

    await userEvent.click(screen.getByRole("button", { name: "Create flow" }));

    const createCall = await waitForCall(client, "/api/v1/flows", "POST");
    expect(createCall.init.json).toMatchObject({
      name: "Sales IVR",
      definition: {
        entry: "node1",
        nodes: {
          node1: { type: "menu", prompt: "Press 1 for sales" },
        },
      },
    });
  });

  it("renders a validation error inline on the offending node", async () => {
    const client = makeStubClient({
      "/api/v1/flows": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") {
          throw new ApiError(
            422,
            "validation_failed",
            "Invalid flow definition: node 'node1' missing required field 'prompt'",
          );
        }
        return [];
      },
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.type(screen.getByLabelText("Flow name"), "Broken flow");
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.selectOptions(screen.getByLabelText("Node type for node1"), "menu");

    await userEvent.click(screen.getByRole("button", { name: "Create flow" }));

    expect(
      await screen.findByText("node 'node1' missing required field 'prompt'"),
    ).toBeInTheDocument();
  });

  it("shows an error with a retry button when the flow list fails to load", async () => {
    let failFlows = true;
    const client = makeStubClient({
      "/api/v1/flows": () => {
        if (failFlows) throw new Error("network down");
        return [FLOW_V1];
      },
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    expect(await screen.findByRole("alert")).toHaveTextContent("network down");

    failFlows = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByRole("button", { name: /Sales/ })).toBeInTheDocument();
  });

  it("omits an empty next field from a speak node's wire payload", async () => {
    const client = makeStubClient({
      "/api/v1/flows": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return CREATED_FLOW;
        return [];
      },
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.type(screen.getByLabelText("Flow name"), "Sales IVR");

    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.selectOptions(screen.getByLabelText("Node type for node1"), "speak");
    await userEvent.type(screen.getByLabelText("Text for node1"), "Hello");
    await userEvent.selectOptions(screen.getByLabelText("Entry node"), "node1");

    await userEvent.click(screen.getByRole("button", { name: "Create flow" }));

    const createCall = await waitForCall(client, "/api/v1/flows", "POST");
    const json = createCall.init.json as { definition: { nodes: Record<string, unknown> } };
    expect(json.definition.nodes.node1).not.toHaveProperty("next");
  });

  it("marks the hours node's fields as required", async () => {
    const client = makeStubClient({
      "/api/v1/flows": [],
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.selectOptions(screen.getByLabelText("Node type for node1"), "hours");

    expect(screen.getByLabelText("Business hours for node1")).toBeRequired();
    expect(screen.getByLabelText("Open node for node1")).toBeRequired();
    expect(screen.getByLabelText("Closed node for node1")).toBeRequired();
    expect(screen.getByLabelText("Holiday node for node1")).toBeRequired();
  });

  it("activates a flow version", async () => {
    const client = makeStubClient({
      "/api/v1/flows/by-name/Sales/versions": [FLOW_V1],
      "/api/v1/flows/flow-1/activate": () => ({ ...FLOW_V1, status: "active" }),
      "/api/v1/flows": [FLOW_V1],
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: /Sales/ }));
    const activateButton = await screen.findByRole("button", { name: "Activate" });
    await userEvent.click(activateButton);

    await waitForCall(client, "/api/v1/flows/flow-1/activate", "POST");
  });

  // Item 6
  it("creates a flow with a transfer node, sending {type: 'transfer', to}", async () => {
    const client = makeStubClient({
      "/api/v1/flows": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return CREATED_FLOW;
        return [];
      },
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.type(screen.getByLabelText("Flow name"), "Sales IVR");

    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.selectOptions(screen.getByLabelText("Node type for node1"), "transfer");
    expect(screen.getByLabelText("Transfer to for node1")).toBeRequired();
    await userEvent.type(screen.getByLabelText("Transfer to for node1"), "+19725550199");
    await userEvent.selectOptions(screen.getByLabelText("Entry node"), "node1");

    await userEvent.click(screen.getByRole("button", { name: "Create flow" }));

    const createCall = await waitForCall(client, "/api/v1/flows", "POST");
    const json = createCall.init.json as { definition: { nodes: Record<string, unknown> } };
    expect(json.definition.nodes.node1).toEqual({ type: "transfer", to: "+19725550199" });
  });

  // Item 6 (round-trip)
  it("round-trips a transfer node loaded from an existing flow version", async () => {
    const flowWithTransfer = {
      ...FLOW_V1,
      definition: {
        entry: "node1",
        nodes: { node1: { type: "transfer", to: "+19725550199" } },
      },
    };
    const client = makeStubClient({
      "/api/v1/flows/by-name/Sales/versions": [flowWithTransfer],
      "/api/v1/flows": [flowWithTransfer],
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: /Sales/ }));

    const toField = await screen.findByLabelText("Transfer to for node1");
    expect(toField).toHaveValue("+19725550199");
  });

  // Item 7/6: a rename now only commits on blur/Enter (see item 6 below) - typing the
  // colliding value in and then blurring is what actually attempts the rename.
  it("rejects renaming a node to an id that already exists", async () => {
    const client = makeStubClient({
      "/api/v1/flows": [],
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));

    // Two nodes now exist: node1 and node2. Renaming node2 to node1 must be rejected.
    const node2IdField = screen.getByLabelText("Node id for node2");
    fireEvent.change(node2IdField, { target: { value: "node1" } });
    fireEvent.blur(node2IdField);

    expect(await screen.findByText('A node named "node1" already exists')).toBeInTheDocument();
    // The rename never applied - node2's own card (and its id field) is still there.
    expect(screen.getByLabelText("Node id for node2")).toBeInTheDocument();
    expect((screen.getByLabelText("Node id for node2") as HTMLInputElement).value).toBe("node1");
  });

  // Item 6: typing a colliding INTERMEDIATE value (here "node1" while renaming node2 to
  // "node1x") must not get stuck - the old per-keystroke commit rejected the mid-typing
  // collision and snapped the controlled input back, making it impossible to ever type
  // past it. Renames now only commit on blur/Enter, so the whole typed value lands at
  // once and only the FINAL value is validated.
  it("allows typing through a value that would collide mid-keystroke, since only blur/Enter commits", async () => {
    const client = makeStubClient({
      "/api/v1/flows": [],
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));

    // node1 and node2 exist. Retype node2's id character by character to "node1x" - the
    // "node1" intermediate value collides with the other node, but since nothing commits
    // until Enter, that never blocks the keystrokes.
    const node2IdField = screen.getByLabelText("Node id for node2");
    await userEvent.clear(node2IdField);
    await userEvent.type(node2IdField, "node1x");
    expect((screen.getByLabelText("Node id for node2") as HTMLInputElement).value).toBe("node1x");

    fireEvent.keyDown(node2IdField, { key: "Enter" });

    // "node1x" doesn't collide with anything - the rename commits, remounting the card
    // under its new id.
    await waitFor(() => expect(screen.queryByLabelText("Node id for node2")).toBeNull());
    expect(screen.getByLabelText("Node id for node1x")).toBeInTheDocument();
    expect(screen.queryByText('A node named "node1" already exists')).toBeNull();
  });

  // Item 6
  it("rejects renaming a node to an empty id, with an inline message, and keeps the card", async () => {
    const client = makeStubClient({
      "/api/v1/flows": [],
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));

    const node1IdField = screen.getByLabelText("Node id for node1");
    await userEvent.clear(node1IdField);
    fireEvent.blur(node1IdField);

    expect(await screen.findByText("Node id cannot be empty")).toBeInTheDocument();
    // The rename never applied - the card is still addressable under its old id.
    expect(screen.getByLabelText("Node id for node1")).toBeInTheDocument();
  });

  // Item 55
  it("labels the remove-option button so it's reachable by accessible name", async () => {
    const client = makeStubClient({
      "/api/v1/flows": [],
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.selectOptions(screen.getByLabelText("Node type for node1"), "menu");
    await userEvent.click(screen.getByRole("button", { name: "Add option" }));

    const removeButton = await screen.findByRole("button", { name: "Remove option 1 for node1" });
    await userEvent.click(removeButton);

    expect(screen.queryByLabelText("Option digit 1 for node1")).not.toBeInTheDocument();
  });

  it("offers the assistant node type in the node type select", async () => {
    const client = makeStubClient({
      "/api/v1/flows": [],
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));

    await screen.findByLabelText("Node type for node1");
    expect(screen.getByRole("option", { name: "assistant" })).toBeInTheDocument();
  });

  it("shows assistant names from the agent profiles API when picking the assistant node", async () => {
    const client = makeStubClient({
      ...EMPTY_LISTS,
      "/api/v1/flows": [],
      "/api/v1/agent/profiles": [
        { id: "p1", name: "Ava" },
        { id: "p2", name: "Ben" },
      ],
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.selectOptions(screen.getByLabelText("Node type for node1"), "assistant");

    expect(await screen.findByLabelText("Assistant for node1")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Ava" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Ben" })).toBeInTheDocument();
  });

  it("tells the user when no assistants exist for an assistant node", async () => {
    const client = makeStubClient({
      "/api/v1/flows": [],
      ...EMPTY_LISTS,
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.selectOptions(screen.getByLabelText("Node type for node1"), "assistant");

    expect(
      await screen.findByText(
        "You have no assistants yet. Create one in Settings, then come back here.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("The assistant answers and handles the call from this point on."),
    ).toBeInTheDocument();
  });

  it("saves an assistant node as {type:'assistant', profile_id}", async () => {
    const client = makeStubClient({
      ...EMPTY_LISTS,
      "/api/v1/flows": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return CREATED_FLOW;
        return [];
      },
      "/api/v1/agent/profiles": [{ id: "p1", name: "Ava" }],
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "New flow" }));
    await userEvent.type(screen.getByLabelText("Flow name"), "Sales IVR");
    await userEvent.click(screen.getByRole("button", { name: "Add node" }));
    await userEvent.selectOptions(screen.getByLabelText("Node type for node1"), "assistant");
    await userEvent.selectOptions(await screen.findByLabelText("Assistant for node1"), "p1");
    await userEvent.selectOptions(screen.getByLabelText("Entry node"), "node1");
    await userEvent.click(screen.getByRole("button", { name: "Create flow" }));

    const createCall = await waitForCall(client, "/api/v1/flows", "POST");
    const json = createCall.init.json as { definition: { nodes: Record<string, unknown> } };
    expect(json.definition.nodes.node1).toEqual({ type: "assistant", profile_id: "p1" });
  });

  it("round-trips an existing assistant node with the chosen assistant preselected", async () => {
    const flowWithAssistant = {
      ...FLOW_V1,
      definition: {
        entry: "node1",
        nodes: { node1: { type: "assistant", profile_id: "p1" } },
      },
    };
    const client = makeStubClient({
      ...EMPTY_LISTS,
      "/api/v1/flows/by-name/Sales/versions": [flowWithAssistant],
      "/api/v1/flows": [flowWithAssistant],
      "/api/v1/agent/profiles": [{ id: "p1", name: "Ava" }],
    });
    renderWithProviders(<FlowsPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: /Sales/ }));

    const picker = await screen.findByLabelText("Assistant for node1");
    expect(picker).toHaveValue("p1");
  });
});
