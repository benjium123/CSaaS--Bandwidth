import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { KnowledgeTab } from "./KnowledgeTab";
import { makeStubClient, renderWithProviders } from "@/test/harness";

function makeDocument(overrides: Record<string, unknown> = {}) {
  return {
    id: "d1",
    title: "Hours",
    source: "text",
    status: "pending",
    chunk_count: 0,
    detail: null,
    ...overrides,
  };
}

describe("KnowledgeTab", () => {
  it("shows what state each document is in", async () => {
    const client = makeStubClient({
      "/api/v1/agent/kb/documents": [
        makeDocument({ id: "d1", title: "Pending", status: "pending" }),
        makeDocument({ id: "d2", title: "Ready doc", status: "indexed", chunk_count: 3 }),
        makeDocument({ id: "d3", title: "Broken", status: "failed" }),
      ],
    });

    renderWithProviders(<KnowledgeTab />, client);

    expect(await screen.findByText("Getting ready")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Could not read it")).toBeInTheDocument();
  });

  it("says so when there is nothing yet", async () => {
    const client = makeStubClient({
      "/api/v1/agent/kb/documents": [],
    });

    renderWithProviders(<KnowledgeTab />, client);

    expect(await screen.findByText("Nothing here yet")).toBeInTheDocument();
  });

  it("pasting text adds a document", async () => {
    let posted: Record<string, unknown> | null = null;
    const client = makeStubClient({
      "/api/v1/agent/kb/documents": (_path: string, init: RequestInit & { json?: unknown }) => {
        if ((init.method ?? "GET") === "POST") {
          posted = init.json as Record<string, unknown>;
          return makeDocument();
        }
        return [];
      },
    });

    renderWithProviders(<KnowledgeTab />, client);

    await userEvent.type(await screen.findByLabelText("Title"), "Hours");
    await userEvent.type(screen.getByLabelText("Text"), "We open at nine.");
    await userEvent.click(screen.getByRole("button", { name: "Add to knowledge" }));

    await waitFor(() => expect(posted).toEqual({ title: "Hours", text: "We open at nine." }));
  });

  it("adding a web page sends the address", async () => {
    let posted: Record<string, unknown> | null = null;
    const client = makeStubClient({
      "/api/v1/agent/kb/documents": (_path: string, init: RequestInit & { json?: unknown }) => {
        if ((init.method ?? "GET") === "POST") {
          posted = init.json as Record<string, unknown>;
          return makeDocument();
        }
        return [];
      },
    });

    renderWithProviders(<KnowledgeTab />, client);

    await userEvent.selectOptions(await screen.findByLabelText("How to add"), "url");
    await userEvent.type(await screen.findByLabelText("Title"), "Pricing");
    await userEvent.type(
      screen.getByLabelText("Web address"),
      "https://example.com/pricing",
    );
    await userEvent.click(screen.getByRole("button", { name: "Add to knowledge" }));

    await waitFor(() =>
      expect(posted).toEqual({
        title: "Pricing",
        url: "https://example.com/pricing",
      }),
    );
  });

  it("uploading a file sends the file itself", async () => {
    // Held in an object rather than two `let`s: TypeScript narrows a `let` to its
    // initialiser and then refuses `.get()` on it, because the assignment happens inside a
    // callback it cannot see running.
    const captured: { body?: FormData; json?: unknown; posted?: boolean } = {};
    const client = makeStubClient({
      "/api/v1/agent/kb/documents": (_path: string, init: RequestInit & { json?: unknown }) => {
        if ((init.method ?? "GET") === "POST") {
          captured.body = init.body as FormData;
          captured.json = init.json;
          captured.posted = true;
          return makeDocument();
        }
        return [];
      },
    });

    renderWithProviders(<KnowledgeTab />, client);

    await userEvent.selectOptions(await screen.findByLabelText("How to add"), "file");
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    await userEvent.upload(await screen.findByLabelText("Choose a file"), file);
    await userEvent.type(await screen.findByLabelText("Title"), "Notes");
    await userEvent.click(screen.getByRole("button", { name: "Add to knowledge" }));

    await waitFor(() => expect(captured.posted).toBe(true));
    expect(captured.body).toBeInstanceOf(FormData);
    expect(captured.body?.get("title")).toBe("Notes");
    const sentFile = captured.body?.get("file") as File;
    expect(sentFile.name).toBe("notes.txt");
    expect(captured.json).toBeUndefined();
  });

  it("a document that could not be read says why", async () => {
    const client = makeStubClient({
      "/api/v1/agent/kb/documents": [
        makeDocument({
          id: "d6",
          title: "Broken",
          status: "failed",
          detail: "The file was sideways.",
        }),
      ],
    });

    renderWithProviders(<KnowledgeTab />, client);

    expect(await screen.findByText("The file was sideways.")).toBeInTheDocument();
  });

  it("removing a document takes two clicks", async () => {
    const doc = makeDocument({
      id: "d1",
      title: "Hours",
      status: "indexed",
      chunk_count: 0,
    });
    const client = makeStubClient({
      "/api/v1/agent/kb/documents": (_path: string, init: RequestInit & { json?: unknown }) => {
        if ((init.method ?? "GET") === "DELETE") return undefined;
        return [doc];
      },
    });

    renderWithProviders(<KnowledgeTab />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Remove Hours" }));
    expect(
      client.calls.some(
        (call) =>
          call.path === "/api/v1/agent/kb/documents/d1" && call.init.method === "DELETE",
      ),
    ).toBe(false);

    await userEvent.click(
      await screen.findByRole("button", { name: "Confirm remove Hours" }),
    );
    await waitFor(() =>
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/agent/kb/documents/d1" && call.init.method === "DELETE",
        ),
      ).toBe(true),
    );
  });

  it("says so when the list cannot be loaded", async () => {
    const client = makeStubClient({
      "/api/v1/agent/kb/documents": new Error("boom"),
    });

    renderWithProviders(<KnowledgeTab />, client);

    const alert = await screen.findByRole("alert");
    expect(alert).toBeInTheDocument();
    expect(screen.getByText("Your knowledge is unavailable.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
