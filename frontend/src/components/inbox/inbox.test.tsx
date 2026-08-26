import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/api/client";
import type { InboxItem } from "@/api/hooks";
import { ThreadList } from "./ThreadList";
import { ThreadView } from "./ThreadView";
import { Composer } from "./Composer";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const ITEM: InboxItem = {
  thread: {
    id: "t1",
    our_e164: "+12145550100",
    contact_e164: "+19725550199",
    status: "open",
    assigned_user_id: null,
    last_message_at: new Date().toISOString(),
  },
  last_message: {
    id: "m1",
    direction: "inbound",
    body: "are you there?",
    status: "received",
    created_at: new Date().toISOString(),
  },
  unread: 2,
  contact: { id: "c1", display_name: "Ada Lovelace" },
  assignee: null,
  labels: [{ id: "l1", name: "urgent", color: "#ef4444" }],
};

describe("ThreadList", () => {
  it("renders the contact name, preview, unread badge and labels", () => {
    const client = makeStubClient({});
    renderWithProviders(
      <ThreadList items={[ITEM]} selectedId={null} onSelect={() => {}} />,
      client,
    );
    expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("are you there?")).toBeInTheDocument();
    expect(screen.getByLabelText("2 unread")).toHaveTextContent("2");
    expect(screen.getByText("urgent")).toBeInTheDocument();
  });

  it("falls back to the formatted phone number when there is no contact", () => {
    const client = makeStubClient({});
    const anon = { ...ITEM, contact: null };
    renderWithProviders(
      <ThreadList items={[anon]} selectedId={null} onSelect={() => {}} />,
      client,
    );
    expect(screen.getByText("(972) 555-0199")).toBeInTheDocument();
  });

  it("reports the selected thread", async () => {
    const onSelect = vi.fn();
    const client = makeStubClient({});
    renderWithProviders(
      <ThreadList items={[ITEM]} selectedId={null} onSelect={onSelect} />,
      client,
    );
    await userEvent.click(screen.getByText("Ada Lovelace"));
    expect(onSelect).toHaveBeenCalledWith("t1");
  });
});

describe("ThreadView", () => {
  it("marks the thread read when opened", async () => {
    const client = makeStubClient({
      "/api/v1/messages": [],
      "/api/v1/threads/t1/read": undefined,
    });
    renderWithProviders(<ThreadView api={client} item={ITEM} />, client);
    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/threads/t1/read")).toBe(true),
    );
  });

  it("renders a failed outbound message with its carrier error code", async () => {
    const client = makeStubClient({
      "/api/v1/threads/t1/read": undefined,
      "/api/v1/messages": [
        {
          id: "m9",
          thread_id: "t1",
          direction: "outbound",
          status: "rejected",
          from_e164: "+12145550100",
          to_e164: "+19725550199",
          body: "nope",
          segment_count_est: 1,
          segment_count_carrier: null,
          error_code: "4720",
          created_at: new Date().toISOString(),
        },
      ],
    });
    renderWithProviders(<ThreadView api={client} item={ITEM} />, client);
    expect(await screen.findByText("4720")).toBeInTheDocument();
    expect(screen.getByLabelText("Failed")).toBeInTheDocument();
  });
});

describe("Composer", () => {
  it("sends the typed body", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSend).toHaveBeenCalledWith("hello", false);
  });

  it("asks before moving a conversation to a new number, then retries with consent", async () => {
    const onSend = vi
      .fn()
      .mockRejectedValueOnce(new ApiError(422, "sticky_sender_unavailable", "retired"))
      .mockResolvedValueOnce(undefined);
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    // It must NOT silently resend from another number.
    expect(await screen.findByRole("alert")).toHaveTextContent("retired");
    expect(onSend).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Send anyway" }));
    await waitFor(() => expect(onSend).toHaveBeenLastCalledWith("hello", true));
  });

  it("shows other errors without offering reassignment", async () => {
    const onSend = vi.fn().mockRejectedValue(new ApiError(422, "validation_failed", "bad number"));
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("bad number");
    expect(screen.queryByRole("button", { name: "Send anyway" })).not.toBeInTheDocument();
  });
});
