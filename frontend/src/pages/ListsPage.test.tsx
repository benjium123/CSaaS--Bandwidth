import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ListsPage } from "./ListsPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const LIST_1 = {
  id: "list-1",
  name: "Q3 buyers",
  source_filename: "buyers.csv",
  status: "ready",
  total_rows: 3,
  accepted_count: 2,
  invalid_count: 1,
  duplicate_count: 0,
  dnc_count: 0,
  error: null,
  created_at: new Date().toISOString(),
};

const PREVIEW = {
  list_id: "list-2",
  name: "leads",
  headers: ["Phone", "First", "Last"],
  preview_rows: [{ Phone: "9725550111", First: "Jane", Last: "Doe" }],
  suggested_mapping: { phone: "Phone", first_name: "First", last_name: "Last" },
  row_count: 1,
};

const COMMITTED_LIST = {
  ...LIST_1,
  id: "list-2",
  name: "leads",
  status: "importing",
  total_rows: 0,
  accepted_count: 0,
};

const ROWS = [
  {
    id: "row-1",
    row_number: 1,
    e164: "+19725550111",
    status: "accepted",
    reason: null,
    fields: { first_name: "Jane" },
    contact_id: "contact-1",
  },
  {
    id: "row-2",
    row_number: 2,
    e164: null,
    status: "invalid",
    reason: "unparseable phone",
    fields: {},
    contact_id: null,
  },
];

const CONSENT_PATH = "/api/v1/compliance/ai-consent/lists/list-1";

function listClient() {
  return makeStubClient({
    "/api/v1/outbound/lists/list-1/rows": ROWS,
    "/api/v1/outbound/lists/list-1": LIST_1,
    "/api/v1/outbound/lists": [LIST_1],
  });
}

async function openList() {
  await userEvent.click(await screen.findByText("Q3 buyers"));
  await screen.findByText("buyers.csv");
}

async function revealConsentForm() {
  await openList();
  await userEvent.click(
    screen.getByRole("button", { name: "Mark list as AI-call consented" }),
  );
}

describe("ListsPage", () => {
  it("renders existing lists and their import report", async () => {
    const client = makeStubClient({
      "/api/v1/outbound/lists/list-1/rows": ROWS,
      "/api/v1/outbound/lists/list-1": LIST_1,
      "/api/v1/outbound/lists": [LIST_1],
    });
    renderWithProviders(<ListsPage />, client);

    expect(await screen.findByText("Q3 buyers")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Q3 buyers"));

    expect(await screen.findByText("buyers.csv")).toBeInTheDocument();
    expect(await screen.findByText("+19725550111")).toBeInTheDocument();
    expect(screen.getByText("unparseable phone")).toBeInTheDocument();
  });

  it("filters rows by status", async () => {
    const client = makeStubClient({
      "/api/v1/outbound/lists/list-1/rows": (path: string) => {
        if (path.includes("status=invalid")) return [ROWS[1]];
        return ROWS;
      },
      "/api/v1/outbound/lists/list-1": LIST_1,
      "/api/v1/outbound/lists": [LIST_1],
    });
    renderWithProviders(<ListsPage />, client);

    await userEvent.click(await screen.findByText("Q3 buyers"));
    await screen.findByText("+19725550111");

    await userEvent.click(screen.getByRole("button", { name: "Invalid" }));

    await waitFor(() => {
      expect(screen.queryByText("+19725550111")).not.toBeInTheDocument();
    });
    expect(screen.getByText("unparseable phone")).toBeInTheDocument();
  });

  it("uploads a file, shows the mapping preview, and commits it", async () => {
    const client = makeStubClient({
      // Registered longest-prefix-first: makeStubClient matches via path.startsWith(key)
      // in object key order, and "/api/v1/outbound/lists/list-2" is itself a prefix of
      // the commit/rows routes below it.
      "/api/v1/outbound/lists/list-2/commit": COMMITTED_LIST,
      "/api/v1/outbound/lists/list-2/rows": [],
      "/api/v1/outbound/lists/list-2": COMMITTED_LIST,
      "/api/v1/outbound/lists": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return PREVIEW;
        return [LIST_1];
      },
    });
    renderWithProviders(<ListsPage />, client);

    await screen.findByText("Q3 buyers");

    const file = new File(["Phone,First,Last\n9725550111,Jane,Doe"], "leads.csv", {
      type: "text/csv",
    });
    await userEvent.upload(screen.getByLabelText("Upload list file"), file);

    expect(await screen.findByText("Map columns: leads")).toBeInTheDocument();
    // Suggested mapping is pre-filled.
    expect(screen.getByLabelText("Map Phone")).toHaveValue("Phone");
    expect(screen.getByLabelText("Map First name")).toHaveValue("First");

    await userEvent.click(screen.getByRole("button", { name: "Commit import" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/outbound/lists/list-2/commit"),
      ).toBe(true),
    );
    const commitCall = client.calls.find(
      (c) => c.path === "/api/v1/outbound/lists/list-2/commit",
    );
    expect(commitCall?.init.json).toEqual({
      mapping: { phone: "Phone", first_name: "First", last_name: "Last" },
    });
  });

  it("disables commit until a phone column is mapped", async () => {
    const noPhonePreview = {
      ...PREVIEW,
      suggested_mapping: { first_name: "First" },
    };
    const client = makeStubClient({
      "/api/v1/outbound/lists": (_path: string, init: RequestInit & { json?: unknown }) => {
        if (init.method === "POST") return noPhonePreview;
        return [];
      },
    });
    renderWithProviders(<ListsPage />, client);

    const file = new File(["First\nJane"], "leads.csv", { type: "text/csv" });
    await userEvent.upload(await screen.findByLabelText("Upload list file"), file);

    await screen.findByText("Map columns: leads");
    expect(screen.getByRole("button", { name: "Commit import" })).toBeDisabled();
  });

  it("reveals the AI-call consent form when the button is clicked", async () => {
    renderWithProviders(<ListsPage />, listClient());

    await openList();
    expect(
      screen.queryByLabelText("Where and when did they agree?"),
    ).not.toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Mark list as AI-call consented" }),
    );

    expect(screen.getByLabelText("Where and when did they agree?")).toBeInTheDocument();
    expect(
      screen.getByLabelText("These contacts gave prior consent to automated calls"),
    ).toBeInTheDocument();
  });

  it("disables Save until both the evidence and the checkbox are filled", async () => {
    renderWithProviders(<ListsPage />, listClient());

    await revealConsentForm();

    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();

    await userEvent.type(
      screen.getByLabelText("Where and when did they agree?"),
      "signup form 2026-05-01",
    );
    expect(save).toBeDisabled();

    await userEvent.click(
      screen.getByLabelText("These contacts gave prior consent to automated calls"),
    );
    expect(save).toBeEnabled();
  });

  it("posts the consent body and shows how many contacts were recorded", async () => {
    const client = makeStubClient({
      "/api/v1/outbound/lists/list-1/rows": ROWS,
      "/api/v1/outbound/lists/list-1": LIST_1,
      "/api/v1/outbound/lists": [LIST_1],
      [CONSENT_PATH]: { list_id: "list-1", recorded: 2 },
    });
    renderWithProviders(<ListsPage />, client);

    await revealConsentForm();

    await userEvent.type(
      screen.getByLabelText("Where and when did they agree?"),
      "signup form 2026-05-01",
    );
    await userEvent.click(
      screen.getByLabelText("These contacts gave prior consent to automated calls"),
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Consent recorded for 2 contacts.")).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Where and when did they agree?"),
    ).not.toBeInTheDocument();

    await waitFor(() =>
      expect(client.calls.some((c) => c.path === CONSENT_PATH)).toBe(true),
    );
    const consentCall = client.calls.find((c) => c.path === CONSENT_PATH);
    expect(consentCall?.init.json).toEqual({
      evidence: "signup form 2026-05-01",
      confirm: true,
    });
  });
});
