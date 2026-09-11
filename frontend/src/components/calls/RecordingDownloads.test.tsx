import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RecordingDownloads } from "./RecordingDownloads";
import { makeStubClient } from "@/test/harness";
import type { RecordingOut } from "@/api/hooks";

let originalCreateObjectURL: typeof URL.createObjectURL;
let originalRevokeObjectURL: typeof URL.revokeObjectURL;

beforeEach(() => {
  originalCreateObjectURL = URL.createObjectURL;
  originalRevokeObjectURL = URL.revokeObjectURL;
  URL.createObjectURL = vi.fn(() => "blob:x");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(new Blob(["x"]), { status: 200 }),
  );
});

afterEach(() => {
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
  vi.restoreAllMocks();
});

describe("RecordingDownloads", () => {
  it("shows one download button for a mixed recording", async () => {
    const client = makeStubClient({});
    const recording = {
      id: "rec-1",
      status: "stored",
      content_type: "audio/wav",
      duration_seconds: 10,
      size_bytes: 100,
      url: "https://x.test/api/v1/calls/c1/recordings/r1",
      channel_layout: "mixed",
      files: [
        {
          layout: "mixed",
          url: "https://x.test/api/v1/calls/c1/recordings/r1?layout=mixed",
        },
      ],
    } as RecordingOut;

    render(<RecordingDownloads api={client} callId="c1" recording={recording} />);

    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveAccessibleName("Download");
    expect(screen.queryByText("Your side")).not.toBeInTheDocument();
  });

  it("offers three downloads for a dual recording and fetches the chosen side", async () => {
    const client = makeStubClient({});
    const recording = {
      id: "rec-2",
      status: "stored",
      content_type: "audio/wav",
      duration_seconds: 20,
      size_bytes: 200,
      url: "https://x.test/api/v1/calls/c1/recordings/r2",
      channel_layout: "dual",
      files: [
        {
          layout: "mixed",
          url: "https://x.test/api/v1/calls/c1/recordings/r2?layout=mixed",
        },
        {
          layout: "agent",
          url: "https://x.test/api/v1/calls/c1/recordings/r2?layout=agent",
        },
        {
          layout: "customer",
          url: "https://x.test/api/v1/calls/c1/recordings/r2?layout=customer",
        },
      ],
    } as RecordingOut;

    render(<RecordingDownloads api={client} callId="c1" recording={recording} />);

    expect(screen.getByRole("button", { name: "Download both sides" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download your side" })).toBeInTheDocument();
    const theirSide = screen.getByRole("button", { name: "Download their side" });
    await userEvent.click(theirSide);

    await waitFor(() => {
      expect(globalThis.fetch).toHaveBeenCalled();
    });
    const fetchSpy = vi.mocked(globalThis.fetch);
    const url = fetchSpy.mock.calls[0][0] as string;
    expect(url).toContain("layout=customer");
  });

  it("renders nothing for a processing recording with no files", () => {
    const client = makeStubClient({});
    const recording = {
      id: "rec-3",
      status: "processing",
      content_type: "audio/wav",
      duration_seconds: null,
      size_bytes: null,
      url: null,
      channel_layout: "mixed",
      files: [],
    } as RecordingOut;

    const { container } = render(
      <RecordingDownloads api={client} callId="c1" recording={recording} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the recording object has no files property", () => {
    const client = makeStubClient({});
    const recording = {
      id: "rec-4",
      status: "stored",
      content_type: "audio/wav",
      duration_seconds: 30,
      size_bytes: 300,
      url: "https://x.test/api/v1/calls/c1/recordings/r4",
      channel_layout: "mixed",
    } as unknown as RecordingOut;

    const { container } = render(
      <RecordingDownloads api={client} callId="c1" recording={recording} />,
    );

    expect(container).toBeEmptyDOMElement();
  });
});
