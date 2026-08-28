import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AgentPage } from "./AgentPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

function makeProfile(overrides: Record<string, unknown> = {}) {
  return {
    id: "p1",
    name: "Main",
    system_prompt: "",
    greeting: "",
    voice_id: "",
    llm_provider: "",
    llm_model: "",
    is_default: false,
    extra: {},
    sms_enabled: false,
    sms_turn_ceiling: 10,
    sms_handoff_keywords: [],
    sms_max_reply_chars: 480,
    ...overrides,
  };
}

describe("AgentPage", () => {
  it("creates, edits, sets default, and deletes a profile end to end", async () => {
    let profiles: Record<string, unknown>[] = [];
    let nextId = 1;

    const client = makeStubClient({
      "/api/v1/agent/profiles": (path: string, init: RequestInit & { json?: unknown }) => {
        const method = init.method ?? "GET";
        const defaultMatch = path.match(/^\/api\/v1\/agent\/profiles\/([^/]+)\/default$/);
        const idMatch = path.match(/^\/api\/v1\/agent\/profiles\/([^/]+)$/);

        if (defaultMatch) {
          const id = defaultMatch[1];
          profiles = profiles.map((p) => ({ ...p, is_default: p.id === id }));
          return profiles.find((p) => p.id === id);
        }
        if (idMatch && method === "PATCH") {
          const id = idMatch[1];
          const body = init.json as Record<string, unknown>;
          profiles = profiles.map((p) => (p.id === id ? { ...p, ...body } : p));
          return profiles.find((p) => p.id === id);
        }
        if (idMatch && method === "DELETE") {
          const id = idMatch[1];
          profiles = profiles.filter((p) => p.id !== id);
          return undefined;
        }
        if (method === "POST") {
          const body = init.json as Record<string, unknown>;
          const created = makeProfile({ id: `p${nextId++}`, ...body });
          profiles = [...profiles, created];
          return created;
        }
        return profiles;
      },
    });

    renderWithProviders(<AgentPage />, client);

    expect(await screen.findByText("No agent profiles yet.")).toBeInTheDocument();

    // Create.
    await userEvent.type(screen.getByLabelText("Profile name"), "Main");
    await userEvent.type(screen.getByLabelText("System prompt"), "You are helpful.");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/agent/profiles" && c.init.method === "POST",
        ),
      ).toBe(true),
    );
    const createCall = client.calls.find(
      (c) => c.path === "/api/v1/agent/profiles" && c.init.method === "POST",
    );
    expect((createCall?.init.json as Record<string, unknown>).name).toBe("Main");

    expect(await screen.findByRole("button", { name: "Main" })).toBeInTheDocument();

    // Edit.
    await userEvent.click(screen.getByRole("button", { name: "Main" }));
    const greetingInput = await screen.findByLabelText("Greeting");
    await userEvent.type(greetingInput, "Hi there");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/agent/profiles/p1" && c.init.method === "PATCH",
        ),
      ).toBe(true),
    );

    // Make default.
    await userEvent.click(screen.getByRole("button", { name: "Make default" }));
    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/agent/profiles/p1/default"),
      ).toBe(true),
    );
    // The button flips to a disabled "Default" once this IS the default profile, and
    // the sidebar badge also reads "Default" - two matches is itself proof the roundtrip
    // updated both the form and the refetched list.
    expect(await screen.findAllByText("Default")).toHaveLength(2);

    // Delete.
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/agent/profiles/p1" && c.init.method === "DELETE",
        ),
      ).toBe(true),
    );
    expect(await screen.findByText("No agent profiles yet.")).toBeInTheDocument();
  });

  it("round-trips the four SMS agent fields", async () => {
    let profiles: Record<string, unknown>[] = [makeProfile()];

    const client = makeStubClient({
      "/api/v1/agent/profiles": (path: string, init: RequestInit & { json?: unknown }) => {
        const method = init.method ?? "GET";
        const idMatch = path.match(/^\/api\/v1\/agent\/profiles\/([^/]+)$/);
        if (idMatch && method === "PATCH") {
          const id = idMatch[1];
          const body = init.json as Record<string, unknown>;
          profiles = profiles.map((p) => (p.id === id ? { ...p, ...body } : p));
          return profiles.find((p) => p.id === id);
        }
        return profiles;
      },
    });

    renderWithProviders(<AgentPage />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));

    await userEvent.click(
      await screen.findByLabelText("Reply to inbound SMS automatically"),
    );

    const turnCeiling = screen.getByLabelText("Turn ceiling");
    await userEvent.clear(turnCeiling);
    await userEvent.type(turnCeiling, "5");

    const maxReplyChars = screen.getByLabelText("Max reply chars");
    await userEvent.clear(maxReplyChars);
    await userEvent.type(maxReplyChars, "300");

    await userEvent.type(
      screen.getByLabelText("Handoff keywords"),
      "human, agent, representative",
    );

    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/agent/profiles/p1" && c.init.method === "PATCH",
        ),
      ).toBe(true),
    );
    const patchCall = client.calls.find(
      (c) => c.path === "/api/v1/agent/profiles/p1" && c.init.method === "PATCH",
    );
    const body = patchCall?.init.json as Record<string, unknown>;
    expect(body.sms_enabled).toBe(true);
    expect(body.sms_turn_ceiling).toBe(5);
    expect(body.sms_max_reply_chars).toBe(300);
    expect(body.sms_handoff_keywords).toEqual(["human", "agent", "representative"]);
  });

  it("creates and deletes a knowledge base document", async () => {
    let documents: Record<string, unknown>[] = [];
    let nextId = 1;

    const client = makeStubClient({
      "/api/v1/agent/profiles": [],
      "/api/v1/kb/documents": (path: string, init: RequestInit & { json?: unknown }) => {
        const method = init.method ?? "GET";
        const idMatch = path.match(/^\/api\/v1\/kb\/documents\/([^/]+)$/);

        if (idMatch && method === "DELETE") {
          const id = idMatch[1];
          documents = documents.filter((d) => d.id !== id);
          return undefined;
        }
        if (idMatch) {
          const id = idMatch[1];
          const doc = documents.find((d) => d.id === id);
          return { ...doc, chunks: [{ seq: 0, text: "We are open weekdays from 9 to 5." }] };
        }
        if (method === "POST") {
          const body = init.json as Record<string, unknown>;
          const created = { id: `d${nextId++}`, source: "pasted", title: body.title };
          documents = [...documents, created];
          return created;
        }
        return documents;
      },
    });

    renderWithProviders(<AgentPage />, client);

    expect(await screen.findByText("No documents yet.")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Document title"), "Hours");
    await userEvent.type(
      screen.getByLabelText("Document text"),
      "We are open weekdays from 9 to 5.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Add document" }));

    expect(await screen.findByRole("button", { name: "Hours" })).toBeInTheDocument();
    const createCall = client.calls.find(
      (c) => c.path === "/api/v1/kb/documents" && c.init.method === "POST",
    );
    expect((createCall?.init.json as Record<string, unknown>).title).toBe("Hours");

    // Expand to view chunks.
    await userEvent.click(screen.getByRole("button", { name: "Hours" }));
    expect(await screen.findByText("We are open weekdays from 9 to 5.")).toBeInTheDocument();

    // Delete.
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/kb/documents/d1" && c.init.method === "DELETE",
        ),
      ).toBe(true),
    );
    expect(await screen.findByText("No documents yet.")).toBeInTheDocument();
  });
});
