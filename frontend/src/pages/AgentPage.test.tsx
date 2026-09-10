import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Assistant } from "@/api/assistants";
import { AgentPage } from "./AgentPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

function makeAssistant(overrides: Partial<Assistant> = {}): Assistant {
  return {
    id: "a1",
    name: "Main",
    system_prompt: "You are a helpful assistant.",
    greeting: "Hello!",
    voice_id: "voice-1",
    llm_provider: "openai",
    llm_model: "gpt-4.1",
    voicemail_message: "Leave a message.",
    is_default: false,
    goals: "Answer the question.",
    guardrails: "Do not be rude.",
    language: "en",
    max_call_seconds: 900,
    silence_timeout_seconds: 12,
    interrupt_sensitivity: "medium",
    voicemail_action: "leave_message",
    tools: [],
    post_call_fields: [],
    effective_prompt: null,
    ...overrides,
  };
}

function makeClient(initialAssistants: Assistant[]) {
  let assistants = initialAssistants.map((assistant) => ({ ...assistant }));
  let nextAssistantNumber = 1;

  return makeStubClient({
    "/api/v1/agent/profiles": (path: string, init: RequestInit & { json?: unknown }) => {
      const method = (init.method ?? "GET").toUpperCase();

      if (path === "/api/v1/agent/profiles" && method === "GET") {
        return assistants.map((assistant) => ({ ...assistant }));
      }

      if (path === "/api/v1/agent/profiles" && method === "POST") {
        const body = (init.json ?? {}) as Partial<Assistant>;
        const created: Assistant = {
          ...makeAssistant(),
          ...body,
          id: `created-${nextAssistantNumber++}`,
          is_default: false,
        };
        assistants.push(created);
        return { ...created };
      }

      if (path.endsWith("/default") && method === "POST") {
        const id = path
          .slice("/api/v1/agent/profiles/".length)
          .replace(/\/default$/, "");
        assistants = assistants.map((assistant) => ({
          ...assistant,
          is_default: assistant.id === id,
        }));
        const updated = assistants.find((assistant) => assistant.id === id);
        if (!updated) throw new Error(`No assistant ${id}`);
        return { ...updated };
      }

      if (path.endsWith("/simulate") && method === "POST") {
        return { reply: "Hello", tokens_in: 1, tokens_out: 1, kb_hits: [] };
      }

      if (method === "PATCH") {
        const id = path
          .slice("/api/v1/agent/profiles/".length)
          .replace(/\/default$/, "");
        const index = assistants.findIndex((assistant) => assistant.id === id);
        if (index < 0) throw new Error(`No assistant ${id}`);
        const body = (init.json ?? {}) as Partial<Assistant>;
        const updated: Assistant = {
          ...assistants[index],
          ...body,
          id: assistants[index].id,
        };
        assistants[index] = updated;
        return { ...updated };
      }

      if (method === "DELETE") {
        const id = path
          .slice("/api/v1/agent/profiles/".length)
          .replace(/\/default$/, "");
        assistants = assistants.filter((assistant) => assistant.id !== id);
        return undefined;
      }

      throw new Error(`Unhandled profiles request ${method} ${path}`);
    },
    "/api/v1/agent/kb/documents": (_path: string, init: RequestInit & { json?: unknown }) => {
      if ((init.method ?? "GET") === "GET") return [];
      throw new Error(`Unhandled kb documents ${String(init.method)} ${_path}`);
    },
  });
}

describe("AgentPage", () => {
  it("creates, renames, sets a default and deletes an assistant end to end", async () => {
    const client = makeClient([]);

    renderWithProviders(<AgentPage />, client);

    await userEvent.type(await screen.findByLabelText("Assistant name"), "Main");
    await userEvent.click(screen.getByRole("button", { name: "Create assistant" }));

    await screen.findByRole("button", { name: "Save persona" });
    const greeting = await screen.findByLabelText("Greeting");
    await userEvent.type(greeting, "Hiya");
    await userEvent.click(screen.getByRole("button", { name: "Save persona" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) => call.init.method === "PATCH",
        ),
      ).toBe(true),
    );

    await userEvent.click(screen.getByRole("button", { name: "Make default" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path.endsWith("/default") && call.init.method === "POST",
        ),
      ).toBe(true),
    );
    await screen.findByRole("button", { name: "Default" });
    await waitFor(() => expect(screen.getAllByText("Default").length).toBe(2));

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm delete?" }));

    expect(await screen.findByText("No assistants yet.")).toBeInTheDocument();
  });

  it("selects the newly created assistant only once it is in the refetched list", async () => {
    const client = makeClient([]);

    renderWithProviders(<AgentPage />, client);

    await userEvent.type(await screen.findByLabelText("Assistant name"), "Main");
    await userEvent.click(screen.getByRole("button", { name: "Create assistant" }));

    const sidebarButton = await screen.findByRole("button", { name: "Main" });
    expect(sidebarButton).toHaveAttribute("aria-current", "true");
  });

  it("opens the tester only for a saved assistant with nothing unsaved", async () => {
    const client = makeClient([makeAssistant()]);

    renderWithProviders(<AgentPage />, client);

    expect(
      screen.queryByRole("button", { name: "Test your assistant" }),
    ).not.toBeInTheDocument();

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("button", { name: "Test your assistant" }));

    expect(
      await screen.findByRole("dialog", { name: "Test your assistant" }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Test your assistant" }),
      ).not.toBeInTheDocument(),
    );

    await userEvent.type(await screen.findByLabelText("Greeting"), "x");
    expect(screen.getByRole("button", { name: "Test your assistant" })).toBeDisabled();
  });

  it("shows the knowledge library inside the builder", async () => {
    const client = makeClient([makeAssistant()]);

    renderWithProviders(<AgentPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("tab", { name: "Knowledge" }));

    expect(
      await screen.findByRole("heading", { name: "Add to your knowledge" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Your assistants share one knowledge library."),
    ).toBeInTheDocument();
  });

  it("warns that there are unsaved changes", async () => {
    const client = makeClient([makeAssistant()]);

    renderWithProviders(<AgentPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.type(await screen.findByLabelText("Greeting"), "Changed");

    expect(await screen.findByText("Unsaved changes")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save persona" }));

    await waitFor(() =>
      expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument(),
    );
  });
});
