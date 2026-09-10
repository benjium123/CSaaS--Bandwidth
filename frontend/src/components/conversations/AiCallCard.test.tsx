import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { AiCallCard, type AiCallCardItem } from "./AiCallCard";

function makeItem(
  overrides: Partial<AiCallCardItem> = {},
  assistantOverrides: Partial<AiCallCardItem["assistant"]> = {},
): AiCallCardItem {
  return {
    id: "call_1",
    direction: "inbound",
    status: "completed",
    duration_seconds: 42,
    occurred_at: new Date().toISOString(),
    failure_detail: null,
    recording: null,
    assistant: {
      summary: "Asked about pricing",
      disposition: "handoff",
      sentiment: "positive",
      has_transcript: true,
      name: "Atlas",
      ...assistantOverrides,
    },
    ...overrides,
  };
}

beforeEach(() => {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:test"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
  Object.defineProperty(window.HTMLMediaElement.prototype, "play", {
    configurable: true,
    value: vi.fn(),
  });
});

describe("AiCallCard", () => {
  it("shows the inbound title", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <AiCallCard item={makeItem({}, { name: null })} api={client} />,
      client,
    );

    expect(screen.getByText("Assistant answered")).toBeTruthy();
  });

  it("shows the outbound title", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <AiCallCard item={makeItem({ direction: "outbound" }, { name: null })} api={client} />,
      client,
    );

    expect(screen.getByText("Assistant called")).toBeTruthy();
  });

  it("includes the assistant name in the title when present", () => {
    const client = makeStubClient({});
    renderWithProviders(<AiCallCard item={makeItem()} api={client} />, client);

    expect(screen.getByText("Assistant answered — Atlas")).toBeTruthy();
  });

  it("omits the assistant name when absent", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <AiCallCard item={makeItem({}, { name: null })} api={client} />,
      client,
    );

    expect(screen.getByText("Assistant answered")).toBeTruthy();
    expect(screen.queryByText(/Assistant answered —/)).toBeNull();
  });

  it("renders the disposition pill in plain words", () => {
    const client = makeStubClient({});
    renderWithProviders(<AiCallCard item={makeItem()} api={client} />, client);

    expect(screen.getByText("Passed to a person")).toBeTruthy();
    expect(screen.queryByText("handoff")).toBeNull();
  });

  it("renders the sentiment pill", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <AiCallCard item={makeItem({}, { sentiment: "positive", name: null })} api={client} />,
      client,
    );

    expect(screen.getByText("Positive")).toBeTruthy();
  });

  it("renders the summary", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <AiCallCard item={makeItem({}, { summary: "Asked about pricing", name: null })} api={client} />,
      client,
    );

    expect(screen.getByText("Asked about pricing")).toBeTruthy();
  });

  it("renders the no-summary fallback", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <AiCallCard item={makeItem({}, { summary: null, name: null })} api={client} />,
      client,
    );

    expect(screen.getByText("No summary for this call.")).toBeTruthy();
  });

  it("does not render a transcript button when has_transcript is false", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <AiCallCard item={makeItem({}, { has_transcript: false, name: null })} api={client} />,
      client,
    );

    expect(screen.queryByRole("button", { name: "Show transcript" })).toBeNull();
  });

  it("does not request the call until the transcript is opened", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      "/api/v1/calls/": (_path: string, _init: RequestInit & { json?: unknown }) => ({
        id: "call_1",
        status: "completed",
        transcript: [{ role: "assistant", text: "Hello", at_ms: 0 }],
      }),
    });

    renderWithProviders(<AiCallCard item={makeItem()} api={client} />, client);

    // AuthProvider issues a request of its own on mount, so this must be scoped to the
    // calls endpoint or it asserts nothing about this card.
    const callRequestCount = () =>
      client.calls.filter(
        (call) =>
          call.path.startsWith("/api/v1/calls/") && (call.init.method ?? "GET") === "GET",
      ).length;

    expect(callRequestCount()).toBe(0);

    await user.click(screen.getByRole("button", { name: "Show transcript" }));

    await waitFor(() => expect(callRequestCount()).toBeGreaterThan(0));
  });

  it("renders transcript roles as Assistant and Caller, never the raw role", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      "/api/v1/calls/": (_path: string, _init: RequestInit & { json?: unknown }) => ({
        id: "call_1",
        status: "completed",
        transcript: [
          { role: "assistant", text: "Hello from assistant", at_ms: 0 },
          { role: "customer", text: "Hello from caller", at_ms: 1000 },
        ],
      }),
    });

    renderWithProviders(<AiCallCard item={makeItem()} api={client} />, client);

    await user.click(screen.getByRole("button", { name: "Show transcript" }));

    await screen.findByText("Hello from assistant");
    expect(screen.getByText("Assistant")).toBeTruthy();
    expect(screen.getByText("Caller")).toBeTruthy();
    expect(screen.queryByText("customer")).toBeNull();
  });

  it("renders the empty-transcript sentence", async () => {
    const user = userEvent.setup();
    const client = makeStubClient({
      "/api/v1/calls/": (_path: string, _init: RequestInit & { json?: unknown }) => ({
        id: "call_1",
        status: "completed",
        transcript: null,
      }),
    });

    renderWithProviders(<AiCallCard item={makeItem()} api={client} />, client);

    await user.click(screen.getByRole("button", { name: "Show transcript" }));

    await screen.findByText("No transcript for this call.");
  });

  it("fetches the recording and renders an audio element", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(new Blob(["x"]), { status: 200 })),
    );

    const client = makeStubClient({});
    const item = makeItem(
      { recording: { id: "rec_1", status: "stored", duration_seconds: null } },
      { has_transcript: false, name: null },
    );

    const { container } = renderWithProviders(<AiCallCard item={item} api={client} />, client);

    await user.click(screen.getByRole("button", { name: "Play recording" }));

    await waitFor(() => expect(container.querySelector("audio")).toBeTruthy());
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/v1/calls/call_1/recordings/rec_1"),
      expect.any(Object),
    );
  });

  it("renders failure detail in a role alert", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <AiCallCard item={makeItem({ failure_detail: "No route" })} api={client} />,
      client,
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("No route");
  });
});
