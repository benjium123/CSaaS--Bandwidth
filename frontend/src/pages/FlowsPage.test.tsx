import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
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
});
