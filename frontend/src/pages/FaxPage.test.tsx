import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FaxPage } from "./FaxPage";
import { ApiError } from "@/api/client";
import { makeStubClient, renderWithProviders } from "@/test/harness";

function faxListFixture(overrides: Record<string, unknown> = {}) {
  return {
    fax_numbers: ["+12145550100"],
    numbers: [
      { id: "n1", e164: "+12145550100", carrier: "telnyx", fax_mode: true },
      { id: "n2", e164: "+12145550101", carrier: "telnyx", fax_mode: false },
    ],
    faxes: [
      {
        id: "f1",
        direction: "outbound",
        status: "delivered",
        from: "+12145550100",
        to: "+12145550199",
        pages: 2,
        charged_micros: 200000,
        failure_reason: null,
        has_document: true,
        document_name: "fax.pdf",
        created_at: "2026-09-20T10:00:00Z",
        completed_at: "2026-09-20T10:01:00Z",
      },
    ],
    ...overrides,
  };
}

describe("FaxPage", () => {
  it("renders history from a fixture", async () => {
    const client = makeStubClient({ "/api/v1/fax": faxListFixture() });
    renderWithProviders(<FaxPage />, client);

    const row = (await screen.findByText("Delivered")).closest("tr");
    expect(row).not.toBeNull();
    const cells = within(row as HTMLElement);
    expect(cells.getByText("Sent")).toBeInTheDocument();
    expect(cells.getByText("2")).toBeInTheDocument();
    expect(cells.getByText("$0.20")).toBeInTheDocument();
    expect(cells.getByRole("button", { name: "Download" })).toBeInTheDocument();
  });

  it("send posts FormData with from, to and the file", async () => {
    const captured: { body?: FormData; posted?: boolean } = {};
    const client = makeStubClient({
      "/api/v1/fax": (_path: string, init: RequestInit & { json?: unknown }) => {
        if ((init.method ?? "GET") === "POST") {
          captured.body = init.body as FormData;
          captured.posted = true;
          return { ...faxListFixture().faxes[0], id: "f2" };
        }
        return faxListFixture();
      },
    });
    renderWithProviders(<FaxPage />, client);

    await screen.findByText("Delivered");
    await userEvent.type(screen.getByLabelText("To"), "+12145550199");
    const file = new File(["%PDF-1.4"], "doc.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText("Document"), file);
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(captured.posted).toBe(true));
    expect(captured.body).toBeInstanceOf(FormData);
    expect(captured.body?.get("from")).toBe("+12145550100");
    expect(captured.body?.get("to")).toBe("+12145550199");
    const sentFile = captured.body?.get("file") as File;
    expect(sentFile.name).toBe("doc.pdf");
  });

  it("shows the credits message on a 402", async () => {
    const client = makeStubClient({
      "/api/v1/fax": (_path: string, init: RequestInit & { json?: unknown }) => {
        if ((init.method ?? "GET") === "POST") {
          throw new ApiError(402, "insufficient_credits", "Not enough balance");
        }
        return faxListFixture();
      },
    });
    renderWithProviders(<FaxPage />, client);

    await screen.findByText("Delivered");
    await userEvent.type(screen.getByLabelText("To"), "+12145550199");
    const file = new File(["%PDF-1.4"], "doc.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText("Document"), file);
    await userEvent.click(screen.getByRole("button", { name: "Send" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Add credits to send faxes");
    const link = within(alert).getByRole("link", { name: "Add credits" });
    expect(link).toHaveAttribute("href", "/settings/billing");
  });

  it("toggling fax mode PATCHes {enabled: true}", async () => {
    const captured: { json?: unknown; patched?: boolean } = {};
    const client = makeStubClient({
      "/api/v1/numbers/n2/fax-mode": (_path: string, init: RequestInit & { json?: unknown }) => {
        captured.json = init.json;
        captured.patched = true;
        return { id: "n2", e164: "+12145550101", fax_mode: true };
      },
      "/api/v1/fax": faxListFixture(),
    });
    renderWithProviders(<FaxPage />, client);

    const toggle = await screen.findByRole("switch", { name: "Fax mode for +12145550101" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    await userEvent.click(toggle);

    await waitFor(() => expect(captured.patched).toBe(true));
    expect(captured.json).toEqual({ enabled: true });
  });
});
