import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/api/client";
import { COMPLIANCE_PREAMBLE, type Assistant } from "@/api/assistants";
import { AssistantsBuilder } from "./AssistantsBuilder";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

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

type MakeClientOptions = {
  initialAssistants?: Assistant[];
  goLive?: RouteStub;
};

function makeClient(options: MakeClientOptions = {}) {
  let assistants = (options.initialAssistants ?? []).map((assistant) => ({ ...assistant }));
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

      if (path.endsWith("/go-live") && method === "POST") {
        if (options.goLive) return options.goLive(path, init);
        return { ok: true };
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

describe("AssistantsBuilder", () => {
  it("creates an assistant from the persona tab", async () => {
    const client = makeClient({ initialAssistants: [] });

    renderWithProviders(<AssistantsBuilder />, client);

    await screen.findByText("No assistants yet.");
    await userEvent.type(screen.getByLabelText("Assistant name"), "Main");
    await userEvent.click(screen.getByRole("button", { name: "Create assistant" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/profiles" && call.init.method === "POST",
        ),
      ).toBe(true),
    );

    const sidebarButton = await screen.findByRole("button", { name: "Main" });
    expect(sidebarButton).toHaveAttribute("aria-current", "true");
    expect(await screen.findByRole("button", { name: "Save persona" })).toBeInTheDocument();
  });

  it("saves only the persona fields", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await waitFor(() => expect(screen.getByLabelText("Greeting")).toHaveValue("Hello!"));

    await userEvent.clear(screen.getByLabelText("Greeting"));
    await userEvent.type(screen.getByLabelText("Greeting"), "Hiya");
    await userEvent.click(screen.getByRole("button", { name: "Save persona" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
        ),
      ).toBe(true),
    );

    const patchCall = client.calls.find(
      (call) =>
        call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
    )!;
    const body = patchCall.init.json as Record<string, unknown>;
    expect(body.greeting).toBe("Hiya");
    expect(body).not.toHaveProperty("system_prompt");
    expect(body).not.toHaveProperty("tools");
    expect(body).not.toHaveProperty("post_call_fields");
  });

  it("saves only the instruction fields", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("tab", { name: "Instructions" }));
    await waitFor(() => expect(screen.getByLabelText("Goals")).toBeInTheDocument());

    await userEvent.clear(screen.getByLabelText("Goals"));
    await userEvent.type(screen.getByLabelText("Goals"), "Be brief.");
    await userEvent.click(screen.getByRole("button", { name: "Save instructions" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
        ),
      ).toBe(true),
    );

    const patchCall = client.calls.find(
      (call) =>
        call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
    )!;
    const body = patchCall.init.json as Record<string, unknown>;
    expect(body.system_prompt).toBe("You are a helpful assistant.");
    expect(body.goals).toBe("Be brief.");
    expect(body.guardrails).toBe("Do not be rude.");
    expect(body).not.toHaveProperty("greeting");
    expect(body).not.toHaveProperty("max_call_seconds");
  });

  it("saves only the behaviour fields", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("tab", { name: "Behaviour" }));

    const minutesInput = await screen.findByLabelText(
      "How long a call can last (minutes)",
    );
    await userEvent.clear(minutesInput);
    await userEvent.type(minutesInput, "5");
    await userEvent.click(screen.getByRole("button", { name: "Save behaviour" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
        ),
      ).toBe(true),
    );

    const patchCall = client.calls.find(
      (call) =>
        call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
    )!;
    const body = patchCall.init.json as Record<string, unknown>;
    expect(body.max_call_seconds).toBe(300);
    expect(body).not.toHaveProperty("name");
    expect(body).not.toHaveProperty("greeting");
    expect(body).not.toHaveProperty("system_prompt");
  });

  it("saves the tools that are switched on", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("tab", { name: "Tools" }));

    const bookAppointment = await screen.findByRole("switch", {
      name: "Book appointments",
    });
    await userEvent.click(bookAppointment);

    expect(bookAppointment).toHaveAttribute("aria-checked", "true");
    await userEvent.click(screen.getByRole("button", { name: "Save tools" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
        ),
      ).toBe(true),
    );

    const patchCall = client.calls.find(
      (call) =>
        call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
    )!;
    const body = patchCall.init.json as Record<string, unknown>;
    expect(body.tools).toEqual([{ tool: "book_appointment" }]);
  });

  it("will not switch on sending information without an address", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("tab", { name: "Tools" }));

    await userEvent.click(
      screen.getByRole("switch", {
        name: "Send information to your own system",
      }),
    );

    expect(
      screen.getByRole("switch", {
        name: "Send information to your own system",
      }),
    ).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("Add an address first.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save tools" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
        ),
      ).toBe(true),
    );

    const patchCall = client.calls.find(
      (call) =>
        call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
    )!;
    const body = patchCall.init.json as Record<string, unknown>;
    expect(body.tools).toEqual([]);
  });

  it("saves an address and a signing secret for your own system", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("tab", { name: "Tools" }));

    await userEvent.click(
      screen.getByRole("button", {
        name: "Set up Send information to your own system",
      }),
    );
    await userEvent.type(
      await screen.findByLabelText("Address"),
      "https://example.com/hook",
    );
    await userEvent.type(screen.getByLabelText("Signing secret"), "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Done" }));

    await userEvent.click(screen.getByRole("button", { name: "Save tools" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
        ),
      ).toBe(true),
    );

    const patchCall = client.calls.find(
      (call) =>
        call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
    )!;
    const body = patchCall.init.json as Record<string, unknown>;
    expect(body.tools).toEqual([
      {
        tool: "webhook",
        url: "https://example.com/hook",
        secret: "s3cret",
      },
    ]);
  });

  it("saves what every call should collect", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("tab", { name: "Outcomes" }));

    await userEvent.click(screen.getByRole("button", { name: "Add a field" }));
    await userEvent.type(await screen.findByLabelText("Field name 1"), "Interested");
    await userEvent.selectOptions(screen.getByLabelText("Field type 1"), "select");
    await userEvent.type(await screen.findByLabelText("Choices 1"), "yes, no");
    await userEvent.type(screen.getByLabelText("Save to contact detail 1"), "interest");
    await userEvent.click(screen.getByRole("button", { name: "Save outcomes" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
        ),
      ).toBe(true),
    );

    const patchCall = client.calls.find(
      (call) =>
        call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
    )!;
    const body = patchCall.init.json as Record<string, unknown>;
    expect(body.post_call_fields).toEqual([
      {
        name: "Interested",
        type: "select",
        options: ["yes", "no"],
        write_to_attribute: "interest",
      },
    ]);
  });

  it("says what is missing when it cannot go live", async () => {
    const client = makeClient({
      initialAssistants: [makeAssistant()],
      goLive: () =>
        new ApiError(422, "not_ready", "Not ready", {
          missing_kinds: ["llm", "tts"],
        }),
    });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("button", { name: "Go live" }));

    expect(
      await screen.findByText("Before this assistant can go live:"),
    ).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "What is missing" })).toBeInTheDocument();
    expect(screen.getByText("Add a language model connection.")).toBeInTheDocument();
    expect(screen.getByText("Add a voice connection.")).toBeInTheDocument();
  });

  it("says so when it does go live", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("button", { name: "Go live" }));

    expect(await screen.findByText("This assistant is live.")).toBeInTheDocument();
    expect(
      screen.queryByText("Before this assistant can go live:"),
    ).not.toBeInTheDocument();
  });

  it("keeps the tabs beyond persona out of reach until the assistant is saved", async () => {
    const client = makeClient({ initialAssistants: [] });

    renderWithProviders(<AssistantsBuilder />, client);

    expect(
      await screen.findByText("Save this assistant first, then you can set up the rest."),
    ).toBeInTheDocument();

    for (const name of ["Instructions", "Knowledge", "Tools", "Behaviour", "Outcomes"]) {
      expect(screen.getByRole("tab", { name })).toBeDisabled();
    }
  });

  it("shows the fixed preamble read-only above the full instructions", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("tab", { name: "Instructions" }));

    const firstEightWords = COMPLIANCE_PREAMBLE.split(" ").slice(0, 8).join(" ");
    // TWO places legitimately carry the preamble: the greyed paragraph that says it is always
    // included, and the read-only preview of everything the server will actually receive.
    // Assert the paragraph specifically, then the preview's value.
    const paragraph = (await screen.findAllByText(new RegExp(firstEightWords))).find(
      (element) => element.tagName === "P",
    );
    expect(paragraph).toBeDefined();
    const fullInstructions = screen.getByLabelText("Full instructions");
    expect(fullInstructions).toHaveAttribute("readonly");
    expect((fullInstructions as HTMLTextAreaElement).value).toContain(firstEightWords);
  });

  it("keeps an in-progress edit when the list refetches in the background", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await waitFor(() => expect(screen.getByLabelText("Greeting")).toHaveValue("Hello!"));

    await userEvent.clear(screen.getByLabelText("Greeting"));
    await userEvent.type(screen.getByLabelText("Greeting"), "Mid-edit");

    // The app fetches the list with no `init` at all, so a GET is `method === undefined`.
    // Filtering on the literal "GET" counts nothing and makes this assertion vacuous.
    const isListGet = (call: { path: string; init: RequestInit }) =>
      call.path === "/api/v1/agent/profiles" && (call.init.method ?? "GET") === "GET";
    const initialGets = client.calls.filter(isListGet).length;

    await userEvent.click(screen.getByRole("button", { name: "Make default" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/profiles/a1/default" &&
            call.init.method === "POST",
        ),
      ).toBe(true),
    );
    await waitFor(() =>
      expect(client.calls.filter(isListGet).length).toBeGreaterThan(initialGets),
    );

    expect(screen.getByLabelText("Greeting")).toHaveValue("Mid-edit");
  });
  // Supervisor-added: the four texting settings came from the OLD agent form, and the
  // rewrite must not quietly drop the SMS agent's only editor. They save with Behaviour.
  it("saves the texting settings the old agent form had", async () => {
    const client = makeClient({ initialAssistants: [makeAssistant()] });

    renderWithProviders(<AssistantsBuilder />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Main" }));
    await userEvent.click(screen.getByRole("tab", { name: "Behaviour" }));

    await userEvent.click(
      await screen.findByLabelText("Reply to inbound texts automatically"),
    );

    const turns = screen.getByLabelText("Most replies in one conversation");
    await userEvent.clear(turns);
    await userEvent.type(turns, "5");

    const chars = screen.getByLabelText("Longest reply (characters)");
    await userEvent.clear(chars);
    await userEvent.type(chars, "300");

    await userEvent.type(
      screen.getByLabelText("Words that hand the conversation to a person"),
      "human, agent",
    );

    await userEvent.click(screen.getByRole("button", { name: "Save behaviour" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
        ),
      ).toBe(true),
    );
    const body = client.calls.find(
      (call) => call.path === "/api/v1/agent/profiles/a1" && call.init.method === "PATCH",
    )!.init.json as Record<string, unknown>;
    expect(body.sms_enabled).toBe(true);
    expect(body.sms_turn_ceiling).toBe(5);
    expect(body.sms_max_reply_chars).toBe(300);
    expect(body.sms_handoff_keywords).toEqual(["human", "agent"]);
  });
});
