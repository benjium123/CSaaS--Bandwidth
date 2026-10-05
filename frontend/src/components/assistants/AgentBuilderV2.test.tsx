import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Assistant } from "@/api/assistants";
import { emptyInterview, type AgentTemplate } from "@/api/agentTemplates";
import { AssistantsBuilder } from "./AssistantsBuilder";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const TEMPLATES: AgentTemplate[] = [
  {
    id: "after_hours",
    version: 1,
    name: "After-Hours Answering",
    channel: "voice",
    summary: "Answers when you are closed.",
    available: true,
    unavailable_reason: null,
    interview: { ...emptyInterview(), goal: "Take a message" },
  },
  {
    id: "missed_call_textback",
    version: 1,
    name: "Missed-Call Text-Back",
    channel: "sms",
    summary: "Texts people you missed.",
    available: true,
    unavailable_reason: null,
    interview: emptyInterview(),
  },
  {
    id: "medical_front_desk",
    version: 1,
    name: "Medical Front Desk",
    channel: "voice",
    summary: "Front desk for clinics.",
    available: false,
    unavailable_reason: "Needs a signed BAA",
    interview: emptyInterview(),
  },
];

function interviewAssistant(overrides: Partial<Assistant> = {}): Assistant {
  return {
    id: "a1",
    name: "Front desk",
    system_prompt: "PROMPT v0",
    greeting: "Hello!",
    voice_id: "v1",
    llm_provider: "openai",
    llm_model: "gpt-4.1",
    voicemail_message: "",
    is_default: false,
    language: "en",
    tools: [],
    post_call_fields: [],
    extra: {
      template_id: "after_hours",
      template_version: 1,
      prompt_mode: "interview",
      interview: { ...emptyInterview(), goal: "Take a message" },
    },
    ...overrides,
  };
}

function makeClient(opts: { assistants?: Assistant[]; features?: Record<string, boolean> } = {}) {
  const assistants = (opts.assistants ?? []).map((a) => ({ ...a }));
  return makeStubClient({
    "/api/v1/me/capabilities": {
      permissions: [],
      org: {},
      features: opts.features ?? {},
    },
    "/api/v1/agent/templates/render": (_p: string, init: { json?: unknown }) => {
      const body = init.json as { interview: { goal: string } };
      return {
        prompt: `PROMPT goal=${body.interview.goal}`,
        greeting: "Hi there",
        locked: ["LOCKED compliance preamble"],
        missing: ["business.name"],
      };
    },
    "/api/v1/agent/templates": TEMPLATES,
    "/api/v1/agent/voices": [
      { id: "v1", name: "Ava", gender: "female", accent: "US", description: "", provider: "elevenlabs" },
      { id: "v2", name: "Ben", gender: "male", accent: "UK", description: "", provider: "elevenlabs" },
    ],
    "/api/v1/agent/profiles/from-template": (_p: string, init: { json?: unknown }) => {
      const body = init.json as { name: string };
      const created = interviewAssistant({ id: "new1", name: body.name });
      assistants.push(created);
      return created;
    },
    "/api/v1/agent/profiles": (path: string, init: RequestInit & { json?: unknown }) => {
      const method = (init.method ?? "GET").toUpperCase();
      if (method === "PATCH") {
        const id = path.split("/").pop();
        const i = assistants.findIndex((a) => a.id === id);
        assistants[i] = { ...assistants[i], ...(init.json as object) };
        return assistants[i];
      }
      return assistants.map((a) => ({ ...a }));
    },
    "/api/v1/agent/kb/documents": [],
  });
}

describe("agent builder v2", () => {
  it("gallery renders templates, blank card, and greys unavailable ones", async () => {
    const client = makeClient();
    renderWithProviders(<AssistantsBuilder />, client);
    await userEvent.click(await screen.findByRole("button", { name: "New" }));

    expect(await screen.findByRole("button", { name: "After-Hours Answering" })).toBeEnabled();
    expect(screen.getByText("Start blank")).toBeInTheDocument();
    expect(screen.getByText("SMS")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Medical Front Desk" })).toBeDisabled();
    expect(screen.getByText("Needs a signed BAA")).toBeInTheDocument();
  });

  it("create flow names the assistant then calls from-template and selects it", async () => {
    const client = makeClient();
    renderWithProviders(<AssistantsBuilder />, client);
    await userEvent.click(await screen.findByRole("button", { name: "New" }));
    await userEvent.click(await screen.findByRole("button", { name: "After-Hours Answering" }));
    await userEvent.type(screen.getByLabelText("Assistant name"), "Night desk");
    await userEvent.click(screen.getByRole("button", { name: "Create assistant" }));

    await waitFor(() => {
      const call = client.calls.find((c) => c.path === "/api/v1/agent/profiles/from-template");
      expect(call).toBeTruthy();
      expect(call!.init.json).toMatchObject({ template_id: "after_hours", name: "Night desk" });
    });
    expect(await screen.findByRole("tab", { name: "Setup" })).toBeInTheDocument();
    expect(await screen.findByLabelText("System prompt")).toBeInTheDocument();
  });

  it("interview edits render the prompt; typing in the prompt goes custom; regenerate restores", async () => {
    const client = makeClient({ assistants: [interviewAssistant()] });
    renderWithProviders(<AssistantsBuilder />, client);
    await userEvent.click(await screen.findByRole("button", { name: /Front desk/ }));

    const prompt = await screen.findByLabelText("System prompt");
    await waitFor(() => expect(prompt).toHaveValue("PROMPT goal=Take a message"));
    expect(screen.getByText("LOCKED compliance preamble")).toBeInTheDocument();
    expect(screen.getByText("business.name")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Ava/ })).toBeInTheDocument();

    const goal = screen.getByLabelText("The one end goal of a conversation");
    await userEvent.clear(goal);
    await userEvent.type(goal, "Book");
    await waitFor(() => expect(prompt).toHaveValue("PROMPT goal=Book"));
    expect(screen.queryByText("Custom prompt")).not.toBeInTheDocument();

    await userEvent.type(prompt, " EDIT");
    expect(screen.getByText("Custom prompt")).toBeInTheDocument();
    expect(prompt).toHaveValue("PROMPT goal=Book EDIT");

    // Interview change in custom mode must not overwrite the typed prompt.
    await userEvent.type(goal, "X");
    await new Promise((r) => setTimeout(r, 600));
    expect(prompt).toHaveValue("PROMPT goal=Book EDIT");

    await userEvent.click(screen.getByRole("button", { name: "Regenerate from interview" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm regenerate" }));
    await waitFor(() => expect(prompt).toHaveValue("PROMPT goal=BookX"));
    expect(screen.queryByText("Custom prompt")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save setup" }));
    await waitFor(() => {
      const patch = client.calls.find((c) => c.init.method === "PATCH");
      expect(patch?.init.json).toMatchObject({
        system_prompt: "PROMPT goal=BookX",
        extra: { prompt_mode: "interview", template_id: "after_hours" },
      });
    });
  });

  it("locked plan disables New and shows the upgrade call to action", async () => {
    const client = makeClient({ features: { ai_agent: false } });
    renderWithProviders(<AssistantsBuilder />, client);
    expect(await screen.findByText("Upgrade to Team")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "New" })).toBeDisabled());
  });
});
