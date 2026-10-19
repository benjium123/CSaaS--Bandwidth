import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageMedia, mediaKind, type MediaItem } from "./MessageMedia";

const photo: MediaItem = {
  url: "https://cdn.example.com/photo.jpg",
  content_type: "image/jpeg",
};

const clip: MediaItem = {
  url: "https://cdn.example.com/clip.mp4",
  content_type: "video/mp4",
};

const pdfItem: MediaItem = {
  url: "https://cdn.example.com/statement.pdf?exp=1&sig=x",
  content_type: "application/pdf",
  size_bytes: 1_200_000,
};

beforeEach(() => {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:test"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("mediaKind", () => {
  it("maps content types", () => {
    expect(mediaKind({ url: "https://cdn.example.com/a", content_type: "image/jpeg" })).toBe(
      "image",
    );
    expect(mediaKind({ url: "https://cdn.example.com/a", content_type: "video/mp4" })).toBe(
      "video",
    );
    expect(mediaKind({ url: "https://cdn.example.com/a", content_type: "audio/mpeg" })).toBe(
      "audio",
    );
    expect(
      mediaKind({ url: "https://cdn.example.com/a", content_type: "application/pdf" }),
    ).toBe("pdf");
    expect(mediaKind({ url: "https://cdn.example.com/a", content_type: "text/vcard" })).toBe(
      "contact",
    );
    expect(mediaKind({ url: "https://cdn.example.com/a", content_type: "text/x-vcard" })).toBe(
      "contact",
    );
    expect(
      mediaKind({ url: "https://cdn.example.com/a", content_type: "application/zip" }),
    ).toBe("file");
  });

  it("falls back to the file extension in the path", () => {
    expect(mediaKind({ url: "https://cdn.example.com/a.pdf?exp=1&sig=x" })).toBe("pdf");
    expect(mediaKind({ url: "https://cdn.example.com/clip.mp4" })).toBe("video");
    expect(mediaKind({ url: "https://cdn.example.com/unknown" })).toBe("file");
  });
});

describe("MessageMedia", () => {
  it("renders nothing for an empty list", () => {
    const { container } = render(<MessageMedia items={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a photo thumbnail and a playable video thumbnail", () => {
    render(<MessageMedia items={[photo, clip]} />);

    const image = screen.getByAltText("Photo attachment");
    expect(image.tagName).toBe("IMG");
    expect(image.getAttribute("src")).toBe(photo.url);
    expect(screen.getByRole("button", { name: "Play video" })).toBeTruthy();
  });

  it("shows the formatted size on an attachment tile", () => {
    render(
      <MessageMedia
        items={[
          {
            url: "https://cdn.example.com/report.pdf",
            content_type: "application/pdf",
            size_bytes: 340_000,
          },
        ]}
      />,
    );

    expect(screen.getByText("340 KB")).toBeTruthy();
  });

  it("opens the viewer from a thumbnail and closes it with the close button", async () => {
    const user = userEvent.setup();
    render(<MessageMedia items={[photo]} />);

    await user.click(screen.getByRole("button", { name: "Open photo" }));
    expect(screen.getByRole("dialog", { name: "Attachment viewer" })).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes on a backdrop click but not when the media itself is clicked", async () => {
    const user = userEvent.setup();
    render(<MessageMedia items={[photo]} />);

    await user.click(screen.getByRole("button", { name: "Open photo" }));
    const dialog = screen.getByRole("dialog", { name: "Attachment viewer" });

    await user.click(within(dialog).getByAltText("Photo attachment"));
    expect(screen.getByRole("dialog", { name: "Attachment viewer" })).toBeTruthy();

    fireEvent.click(dialog);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("navigates with the arrow keys and closes on Escape", async () => {
    const user = userEvent.setup();
    render(<MessageMedia items={[photo, clip]} />);

    await user.click(screen.getByRole("button", { name: "Open photo" }));
    expect(screen.getByText("1 / 2")).toBeTruthy();

    await user.keyboard("{ArrowRight}");
    expect(screen.getByText("2 / 2")).toBeTruthy();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renders a PDF through a blob URL", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => ({
      ok: true,
      arrayBuffer: async () => new ArrayBuffer(8),
    }));
    vi.stubGlobal("fetch", fetchMock);

    render(<MessageMedia items={[pdfItem]} />);
    await user.click(screen.getByRole("button", { name: "Open PDF" }));

    const frame = await screen.findByTitle("PDF attachment");
    expect(frame.getAttribute("src")).toBe("blob:test");
    expect(fetchMock).toHaveBeenCalled();
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it("swaps a broken photo for the generic attachment tile", () => {
    render(<MessageMedia items={[photo]} />);

    fireEvent.error(screen.getByAltText("Photo attachment"));

    expect(screen.getByText("Attachment")).toBeTruthy();
    expect(screen.queryByAltText("Photo attachment")).toBeNull();
  });
});
