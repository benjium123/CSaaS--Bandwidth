import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Composer } from "./Composer";
import { makeStubClient, renderWithProviders } from "@/test/harness";

function oversizedImage(): File {
  const file = new File(["x"], "big.png", { type: "image/png" });
  Object.defineProperty(file, "size", { value: 4_000_000 });
  return file;
}

function localDateTimeValue(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}T${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

describe("Composer P28 messaging", () => {
  it("an oversized file is refused instantly and nothing is uploaded", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.upload(
      screen.getByLabelText("Choose files to attach"),
      oversizedImage(),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("Attachments up to 3.7 MB");
    expect(client.calls.some((call) => call.path === "/api/v1/media")).toBe(false);
  });

  it("a file the network cannot send is refused by kind", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} />, client);

    // NOT userEvent.upload here: it honours the input's `accept` list and silently drops
    // a file that does not match, so the guard under test would never even be reached.
    // A real file dialog can still hand us one (the user picks "All files"), which is
    // exactly the case this check exists for - so hand it one directly.
    fireEvent.change(screen.getByLabelText("Choose files to attach"), {
      target: { files: [new File(["x"], "archive.zip", { type: "application/zip" })] },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That kind of file can't be sent in a message.",
    );
    expect(client.calls.some((call) => call.path === "/api/v1/media")).toBe(false);
  });

  it("an accepted attachment uploads and its id rides along on the send", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({
      "/api/v1/media": {
        id: "media-1",
        content_type: "image/png",
        size_bytes: 10,
        status: "stored",
        url: null,
      },
    });

    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.upload(
      screen.getByLabelText("Choose files to attach"),
      new File(["x"], "small.png", { type: "image/png" }),
    );

    await screen.findByRole("button", { name: "Remove small.png" });
    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(onSend).toHaveBeenCalledWith(
      "hello",
      false,
      expect.objectContaining({ media_ids: ["media-1"] }),
    );
  });

  it("removing an attachment drops it from the send", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({
      "/api/v1/media": {
        id: "media-1",
        content_type: "image/png",
        size_bytes: 10,
        status: "stored",
        url: null,
      },
    });

    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.upload(
      screen.getByLabelText("Choose files to attach"),
      new File(["x"], "small.png", { type: "image/png" }),
    );

    const removeButton = await screen.findByRole("button", {
      name: "Remove small.png",
    });
    await userEvent.click(removeButton);

    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(onSend).toHaveBeenCalledWith("hello", false);
    expect(onSend.mock.calls[0][2]).toBeUndefined();
  });

  it("a time in the past is refused and nothing is sent", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.click(screen.getByRole("button", { name: "Send later" }));

    const past = new Date(Date.now() - 24 * 60 * 60 * 1000);
    fireEvent.change(screen.getByLabelText("Send at"), {
      target: { value: localDateTimeValue(past) },
    });

    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Schedule" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Pick a time in the future to send this later",
    );
    expect(onSend).not.toHaveBeenCalled();
  });

  it("a future time is sent as an absolute instant and the button says Schedule", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.click(screen.getByRole("button", { name: "Send later" }));

    const future = new Date(Date.now() + 24 * 60 * 60 * 1000);
    const futureValue = localDateTimeValue(future);
    fireEvent.change(screen.getByLabelText("Send at"), {
      target: { value: futureValue },
    });

    expect(screen.getByRole("button", { name: "Schedule" })).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Schedule" }));

    await waitFor(() => expect(onSend).toHaveBeenCalled());
    const extras = onSend.mock.calls[0][2];
    expect(extras.scheduled_for).toBe(new Date(futureValue).toISOString());
    expect(new Date(extras.scheduled_for).getTime()).toBeGreaterThan(Date.now());
  });

  it("the tracking toggle only exists when the body has a real link", async () => {
    const client = makeStubClient({});
    renderWithProviders(
      <Composer onSend={vi.fn().mockResolvedValue(undefined)} />,
      client,
    );

    const field = screen.getByLabelText("Message");

    await userEvent.type(field, "no link here");
    expect(
      screen.queryByRole("checkbox", { name: "Track link clicks" }),
    ).toBeNull();

    await userEvent.clear(field);
    await userEvent.type(field, "see example.com");
    expect(
      screen.queryByRole("checkbox", { name: "Track link clicks" }),
    ).toBeNull();

    await userEvent.clear(field);
    await userEvent.type(field, "see https://example.com/x");
    const checkbox = screen.getByRole("checkbox", { name: "Track link clicks" });
    expect(checkbox).toBeInTheDocument();
    expect(checkbox).not.toBeChecked();
    expect(screen.queryByText(/swap the link for a trackable one/)).toBeNull();
  });

  it("checking the toggle promises tracking and sends track_links", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.type(
      screen.getByLabelText("Message"),
      "see https://example.com/x",
    );
    await userEvent.click(
      screen.getByRole("checkbox", { name: "Track link clicks" }),
    );

    expect(
      screen.getByText(/We’ll swap the link for a trackable one/),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(onSend).toHaveBeenCalledWith(
      "see https://example.com/x",
      false,
      expect.objectContaining({ track_links: true }),
    );
  });

  it("an untouched composer sends exactly what it always did", async () => {
    const onSend = vi.fn().mockResolvedValue(undefined);
    const client = makeStubClient({});
    renderWithProviders(<Composer onSend={onSend} />, client);

    await userEvent.type(screen.getByLabelText("Message"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(onSend).toHaveBeenCalledWith("hello", false);
    expect(onSend.mock.calls[0][2]).toBeUndefined();
  });
});
