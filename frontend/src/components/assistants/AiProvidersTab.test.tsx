import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AiProvidersTab } from "./AiProvidersTab";
import { makeStubClient, renderWithProviders } from "@/test/harness";

function makeSettings(overrides: Record<string, unknown> = {}) {
  return { ai_key_mode: "platform", byok_ready: false, missing_kinds: [], ...overrides };
}

function makeAccount(overrides: Record<string, unknown> = {}) {
  return {
    id: "p1",
    kind: "llm",
    provider: "openai",
    label: "Main model",
    status: "unverified",
    last_probe_at: null,
    last_probe_detail: null,
    fields: { api_key: true },
    has_credentials: false,
    ...overrides,
  };
}

describe("AiProvidersTab", () => {
  it("shows the current mode with the chosen option checked", async () => {
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings(),
      "/api/v1/ai/providers": [],
    });

    renderWithProviders(<AiProvidersTab />, client);

    const csaaS = await screen.findByRole("radio", {
      name: "Use CSaaS keys (billed per use)",
    });
    expect(csaaS).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Use my own keys" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("switching to your own keys saves the new mode", async () => {
    const client = makeStubClient({
      "/api/v1/ai/settings": (_path: string, init: RequestInit & { json?: unknown }) => {
        if ((init.method ?? "GET") === "PATCH") {
          const body = init.json as { ai_key_mode: string };
          return {
            ai_key_mode: body.ai_key_mode,
            byok_ready: false,
            missing_kinds: ["llm", "stt", "tts"],
          };
        }
        return makeSettings();
      },
      "/api/v1/ai/providers": [],
    });

    renderWithProviders(<AiProvidersTab />, client);

    await screen.findByRole("radio", { name: "Use my own keys" });
    await userEvent.click(screen.getByRole("radio", { name: "Use my own keys" }));

    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/ai/settings" &&
            call.init.method === "PATCH" &&
            (call.init.json as { ai_key_mode: string }).ai_key_mode === "byok",
        ),
      ).toBe(true),
    );
  });

  it("clicking the mode that is already chosen sends nothing", async () => {
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings(),
      "/api/v1/ai/providers": [],
    });

    renderWithProviders(<AiProvidersTab />, client);

    await screen.findByText("Using CSaaS keys");
    await userEvent.click(
      screen.getByRole("radio", { name: "Use CSaaS keys (billed per use)" }),
    );

    expect(
      client.calls.some(
        (call) => call.path === "/api/v1/ai/settings" && call.init.method === "PATCH",
      ),
    ).toBe(false);
  });

  it("platform mode never shows a missing list", async () => {
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings(),
      "/api/v1/ai/providers": [],
    });

    renderWithProviders(<AiProvidersTab />, client);

    expect(await screen.findByText("Using CSaaS keys")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "What is still needed" })).toBeNull();
  });

  it("byok mode names what is still missing in plain words", async () => {
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings({
        ai_key_mode: "byok",
        byok_ready: false,
        missing_kinds: ["llm", "tts"],
      }),
      "/api/v1/ai/providers": [],
    });

    renderWithProviders(<AiProvidersTab />, client);

    expect(await screen.findByText("Still needed")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "What is still needed" });
    expect(within(list).getByText("Add a language model connection.")).toBeInTheDocument();
    expect(within(list).getByText("Add a voice connection.")).toBeInTheDocument();
  });

  it("byok mode says ready when nothing is missing", async () => {
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings({
        ai_key_mode: "byok",
        byok_ready: true,
        missing_kinds: [],
      }),
      "/api/v1/ai/providers": [],
    });

    renderWithProviders(<AiProvidersTab />, client);

    expect(await screen.findByText("Ready")).toBeInTheDocument();
  });

  it("the provider list only offers providers for the chosen purpose", async () => {
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings(),
      "/api/v1/ai/providers": [],
    });

    renderWithProviders(<AiProvidersTab />, client);

    const purposeSelect = await screen.findByLabelText("What this connection is for");
    await userEvent.selectOptions(purposeSelect, "tts");

    const providerSelect = await screen.findByLabelText("Connect a provider");
    expect(within(providerSelect).getByRole("option", { name: "ElevenLabs" })).toBeInTheDocument();
    expect(within(providerSelect).getByRole("option", { name: "Cartesia" })).toBeInTheDocument();
    expect(within(providerSelect).queryByRole("option", { name: "OpenAI" })).not.toBeInTheDocument();
  });

  it("only the chosen provider's fields are shown", async () => {
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings(),
      "/api/v1/ai/providers": [],
    });

    renderWithProviders(<AiProvidersTab />, client);

    await userEvent.selectOptions(
      await screen.findByLabelText("What this connection is for"),
      "tts",
    );
    await userEvent.selectOptions(
      await screen.findByLabelText("Connect a provider"),
      "elevenlabs",
    );

    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    expect(screen.getByLabelText("Default voice id")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Connect a provider"), "cartesia");

    await waitFor(() =>
      expect(screen.queryByLabelText("Default voice id")).not.toBeInTheDocument(),
    );
  });

  it("saving a new connection posts the kind, provider and credentials", async () => {
    let posted: Record<string, unknown> | null = null;
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings(),
      "/api/v1/ai/providers": (path: string, init: RequestInit & { json?: unknown }) => {
        if (path.endsWith("/probe")) {
          return { status: "active", detail: "ok" };
        }
        if ((init.method ?? "GET") === "POST") {
          posted = init.json as Record<string, unknown>;
          return {
            id: "p1",
            kind: posted.kind,
            provider: posted.provider,
            label: posted.label ?? "",
            status: "unverified",
            last_probe_at: null,
            last_probe_detail: null,
            fields: { api_key: true, default_voice_id: false },
            has_credentials: false,
          };
        }
        return [];
      },
    });

    renderWithProviders(<AiProvidersTab />, client);

    await userEvent.selectOptions(
      await screen.findByLabelText("What this connection is for"),
      "tts",
    );
    await userEvent.selectOptions(
      await screen.findByLabelText("Connect a provider"),
      "elevenlabs",
    );

    await userEvent.type(screen.getByLabelText("API key"), "sk-live");
    await userEvent.type(screen.getByLabelText("Default voice id"), "rachel");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toEqual({
      kind: "tts",
      provider: "elevenlabs",
      label: "ElevenLabs",
      credentials: { api_key: "sk-live", default_voice_id: "rachel" },
    });
  });

  it("a stored secret is never shown", async () => {
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings(),
      "/api/v1/ai/providers": [makeAccount({ has_credentials: true })],
    });

    renderWithProviders(<AiProvidersTab />, client);

    await screen.findByText("Main model");
    const apiKeyInput = screen.getByLabelText("API key") as HTMLInputElement;

    expect(apiKeyInput).toHaveValue("");
    expect(apiKeyInput).toHaveAttribute("placeholder", "stored — leave blank to keep");
    expect(screen.getByText("Key saved")).toBeInTheDocument();
  });

  it("testing a connection shows what came back", async () => {
    let account = makeAccount({ last_probe_detail: "Before" });
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings(),
      "/api/v1/ai/providers": (path: string) => {
        if (path.endsWith("/probe")) {
          account = makeAccount({
            status: "active",
            last_probe_detail: "Probe says hello",
          });
          return { status: "active", detail: "Probe says hello" };
        }
        return [account];
      },
    });

    renderWithProviders(<AiProvidersTab />, client);

    await screen.findByRole("button", { name: "Test Main model" });
    expect(screen.queryByText("Probe says hello")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Test Main model" }));

    expect(await screen.findByText("Probe says hello")).toBeInTheDocument();
  });

  it("removing a connection takes two clicks", async () => {
    const client = makeStubClient({
      "/api/v1/ai/settings": makeSettings(),
      "/api/v1/ai/providers": (_path: string, init: RequestInit & { json?: unknown }) => {
        if ((init.method ?? "GET") === "DELETE") {
          return undefined;
        }
        return [makeAccount()];
      },
    });

    renderWithProviders(<AiProvidersTab />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Remove Main model" }));
    expect(
      client.calls.some(
        (call) => call.path === "/api/v1/ai/providers/p1" && call.init.method === "DELETE",
      ),
    ).toBe(false);

    await userEvent.click(
      await screen.findByRole("button", { name: "Confirm remove Main model" }),
    );
    await waitFor(() =>
      expect(
        client.calls.some(
          (call) => call.path === "/api/v1/ai/providers/p1" && call.init.method === "DELETE",
        ),
      ).toBe(true),
    );
  });
});
