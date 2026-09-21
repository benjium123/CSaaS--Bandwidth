import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QRCodeSVG } from "qrcode.react";
import { TotpEnrolment } from "./TotpEnrolment";

const SECRET = "JBSWY3DPEHPK3PXP";
const URI = "otpauth://totp/Ringlite:a@example.com?secret=JBSWY3DPEHPK3PXP&issuer=Ringlite";

/**
 * The `d` of the LAST <path> under `root` - qrcode.react emits the foreground geometry
 * there, so two codes encoding different payloads differ in exactly this string.
 */
function pathD(root: Element): string {
  const paths = root.querySelectorAll("path");
  const last = paths[paths.length - 1];
  return last ? last.getAttribute("d") ?? "" : "";
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("TotpEnrolment", () => {
  it("renders a single QR once enrolment data is present", () => {
    const { container } = render(<TotpEnrolment secret={SECRET} uri={URI} />);

    const qr = screen.getByTestId("totp-qr");
    expect(qr).toBeInTheDocument();
    expect(qr.tagName.toLowerCase()).toBe("svg");
    // A zero-size SVG would render nothing scannable: qrcode.react sets the size as SVG
    // width/height presentation attributes, so pin that here.
    expect(qr.getAttribute("width")).toBe("176");
    expect(qr.getAttribute("height")).toBe("176");
    expect(container.querySelectorAll('[data-testid="totp-qr"]').length).toBe(1);
  });

  it("encodes the provisioning URI, not the bare secret", () => {
    // Reference geometry straight from the library, rendered the same way, so this cannot
    // pass tautologically: the component's path must equal the URI code and NOT the secret
    // code.
    const { container } = render(<TotpEnrolment secret={SECRET} uri={URI} />);
    const refUri = render(
      <QRCodeSVG
        value={URI}
        size={176}
        level="M"
        bgColor="#ffffff"
        fgColor="#0b0f14"
        marginSize={4}
      />,
    ).container;
    const refSecret = render(
      <QRCodeSVG
        value={SECRET}
        size={176}
        level="M"
        bgColor="#ffffff"
        fgColor="#0b0f14"
        marginSize={4}
      />,
    ).container;

    const fg = container.querySelector('[data-testid="totp-qr"] path');
    expect(fg).not.toBeNull();

    const actual = pathD(container);
    expect(actual).not.toBe("");
    expect(actual).toBe(pathD(refUri));
    expect(actual).not.toBe(pathD(refSecret));
  });

  it("still shows the manual key, grouped for reading", () => {
    render(<TotpEnrolment secret={SECRET} uri={URI} />);

    const key = screen.getByTestId("totp-secret");
    expect(key).toHaveAttribute("data-secret", SECRET);
    expect((key.textContent ?? "").replace(/\s/g, "")).toBe(SECRET);
  });

  it("copies the exact secret, with no grouping spaces", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<TotpEnrolment secret={SECRET} uri={URI} />);
    await userEvent.click(screen.getByRole("button", { name: "Copy key" }));

    expect(writeText).toHaveBeenCalledWith(SECRET);
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("survives a clipboard rejection, never sticks on Copied, and clears the failure on a later success", async () => {
    // Reject once, then resolve - so one render proves both the failed path and that a
    // subsequent successful copy clears the failure message.
    const writeText = vi
      .fn()
      .mockRejectedValueOnce(new Error("NotAllowedError"))
      .mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<TotpEnrolment secret={SECRET} uri={URI} />);
    // Must resolve, not reject: the handler catches the rejected writeText.
    await userEvent.click(screen.getByRole("button", { name: "Copy key" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Couldn't copy automatically - select the key and copy it.",
    );
    expect(screen.getByRole("button", { name: "Copy key" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copied" })).toBeNull();

    const key = screen.getByTestId("totp-secret");
    expect(key).toBeInTheDocument();
    expect(key.className).toContain("select-all");

    // Second click succeeds: the failure message must clear and the button show Copied.
    await userEvent.click(screen.getByRole("button", { name: "Copy key" }));
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("generates the QR with no network request and leaks nothing off-origin", () => {
    // Guards the one mutation this page cannot afford: swapping the LOCAL render for a
    // remote image, e.g.
    //   <img src="https://api.qrserver.com/v1/create-qr-code/?data=otpauth://...">
    // jsdom never fetches <img src>, so the network spies alone would stay green - the DOM
    // attribute scan below is the assertion that actually goes RED for that swap.
    if (!globalThis.fetch) vi.stubGlobal("fetch", vi.fn());
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const xhrSpy = vi.spyOn(XMLHttpRequest.prototype, "open");

    const { container } = render(<TotpEnrolment secret={SECRET} uri={URI} />);

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(xhrSpy).not.toHaveBeenCalled();

    const REMOTE = /(^|["'\s(])(https?:)?\/\/[^/]/i;
    for (const el of Array.from(container.querySelectorAll("*"))) {
      for (const attr of Array.from(el.attributes)) {
        expect(attr.value, `${el.tagName}[${attr.name}]`).not.toMatch(REMOTE);
      }
    }

    expect(container.querySelectorAll("img, iframe, script, object, embed").length).toBe(0);
  });
});
