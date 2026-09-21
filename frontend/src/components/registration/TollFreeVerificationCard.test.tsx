import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TollFreeVerificationCard } from "./TollFreeVerificationCard";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const TFV_PATH = "/api/v1/registration/tollfree";
const SUBMIT_SUFFIX = "/submit";

const CAPABILITIES_ORG = {
  has_provider: true,
  has_number: true,
  member_count: 2,
  registration_state: "approved",
};

const COMPLIANCE_MANAGE = ["compliance:read", "compliance:manage"];

/**
 * The production wrappers wait for a known account type before rendering, so the
 * AuthProvider's /auth/me must be a real signed-in business identity: full_name plus an
 * org-1 owner/business membership.
 */
const ME_BUSINESS = {
  id: "u-1",
  email: "u@example.com",
  full_name: "Test User",
  permissions: [],
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org One",
      org_slug: "org-one",
      role_name: "owner",
      account_type: "business",
    },
  ],
};

type NumberRow = {
  id: string;
  e164: string;
  number_type: string;
  status: string;
};

type TfvRow = {
  id: string;
  number_id: string;
  business_name: string;
  status: string;
  last_error: string | null;
};

function makeNumber(id: string, e164: string, numberType: string): NumberRow {
  return { id, e164, number_type: numberType, status: "active" };
}

const APPROVED_ROW: TfvRow = {
  id: "tfv-approved",
  number_id: "num-approved",
  business_name: "Approved Biz",
  status: "approved",
  last_error: null,
};

const REJECTED_ROW: TfvRow = {
  id: "tfv-rejected",
  number_id: "num-rejected",
  business_name: "Rejected Biz",
  status: "rejected",
  last_error: "Carrier rejected the opt-in proof",
};

const DRAFT_ROW: TfvRow = {
  id: "tfv-draft",
  number_id: "num-draft",
  business_name: "Draft Biz",
  status: "draft",
  last_error: null,
};

const ALL_STATUS_ROWS: TfvRow[] = [APPROVED_ROW, REJECTED_ROW, DRAFT_ROW];

/**
 * One stub for the whole TFV domain: the harness matches by longest prefix, so GET list,
 * POST create and POST submit all land here and are told apart by method and suffix.
 *
 * The list is COPIED into a mutable local array so a successful /submit can advance the
 * row in place: the component invalidates the list on success, and the follow-up GET has to
 * see the new status or the mutation looks like a no-op.
 */
function routesFor(options: {
  permissions?: string[];
  numbers?: NumberRow[];
  verifications?: TfvRow[];
}): Record<string, RouteStub | unknown> {
  const permissions = options.permissions ?? COMPLIANCE_MANAGE;
  const numbers = options.numbers ?? [];
  const verifications: TfvRow[] = [...(options.verifications ?? [])];

  const tfvRoute: RouteStub = (path, init) => {
    if ((init.method ?? "GET") === "POST") {
      if (path.endsWith(SUBMIT_SUFFIX)) {
        // `/api/v1/registration/tollfree/<id>/submit` -> strip the prefix and the suffix to
        // recover the raw id.
        const id = path.slice(
          TFV_PATH.length + 1,
          -SUBMIT_SUFFIX.length,
        );
        const index = verifications.findIndex((candidate) => candidate.id === id);
        const row = index >= 0 ? verifications[index] : undefined;
        if (row !== undefined) {
          const updated: TfvRow = { ...row, status: "submitted" };
          verifications.splice(index, 1, updated);
          return updated;
        }
        return {
          id,
          number_id: "",
          business_name: "",
          status: "submitted",
          last_error: null,
        };
      }
      return {
        id: "tfv-created",
        number_id: "num-created",
        business_name: "Created Co",
        status: "draft",
        last_error: null,
      };
    }
    return verifications;
  };

  return {
    "/api/v1/auth/me": ME_BUSINESS,
    "/api/v1/me/capabilities": { permissions, org: CAPABILITIES_ORG },
    "/api/v1/numbers": numbers,
    [TFV_PATH]: tfvRoute,
  };
}

describe("TollFreeVerificationCard", () => {
  it("renders the create form for a user with compliance:manage", async () => {
    const client = makeStubClient(
      routesFor({ numbers: [makeNumber("num-tf-1", "+18005550100", "tollfree")] }),
    );
    renderWithProviders(<TollFreeVerificationCard />, client);

    expect(await screen.findByLabelText("Toll-free number")).toBeInTheDocument();
    expect(screen.getByLabelText("Business name")).toBeInTheDocument();
    expect(screen.getByLabelText("Use case summary")).toBeInTheDocument();
    expect(screen.getByLabelText("Opt-in process")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Create verification" }),
    ).toBeInTheDocument();
  });

  it("creates a verification with the exact payload", async () => {
    const client = makeStubClient(
      routesFor({ numbers: [makeNumber("num-tf-1", "+18005550100", "tollfree")] }),
    );
    renderWithProviders(<TollFreeVerificationCard />, client);

    await userEvent.selectOptions(
      await screen.findByLabelText("Toll-free number"),
      "num-tf-1",
    );
    await userEvent.type(screen.getByLabelText("Business name"), "Acme Corp");
    await userEvent.selectOptions(screen.getByLabelText("Use case"), "MARKETING");
    await userEvent.type(
      screen.getByLabelText("Use case summary"),
      "Weekly promo blasts",
    );
    await userEvent.type(
      screen.getByLabelText("Opt-in process"),
      "Text START to 8005550100",
    );
    await userEvent.type(
      screen.getByLabelText("Opt-in screenshot URL"),
      "https://example.com/optin.png",
    );
    await userEvent.type(screen.getByLabelText("Monthly message volume"), "1500");
    await userEvent.type(screen.getByLabelText("Contact email"), "ops@example.com");

    await userEvent.click(screen.getByRole("button", { name: "Create verification" }));

    await waitFor(() => {
      const call = client.calls.find(
        (candidate) =>
          candidate.path === TFV_PATH && (candidate.init.method ?? "GET") === "POST",
      );
      expect(call).toBeDefined();
      expect(call?.init.json).toEqual({
        number_id: "num-tf-1",
        business_name: "Acme Corp",
        use_case: "MARKETING",
        use_case_summary: "Weekly promo blasts",
        opt_in_process: "Text START to 8005550100",
        opt_in_screenshot_url: "https://example.com/optin.png",
        message_volume: 1500,
        contact_email: "ops@example.com",
      });
    });
  });

  it("omits blank optional fields from the payload", async () => {
    const client = makeStubClient(
      routesFor({ numbers: [makeNumber("num-tf-1", "+18005550100", "tollfree")] }),
    );
    renderWithProviders(<TollFreeVerificationCard />, client);

    await userEvent.selectOptions(
      await screen.findByLabelText("Toll-free number"),
      "num-tf-1",
    );
    await userEvent.type(screen.getByLabelText("Business name"), "Acme Corp");
    await userEvent.type(
      screen.getByLabelText("Use case summary"),
      "Weekly promo blasts",
    );
    await userEvent.type(
      screen.getByLabelText("Opt-in process"),
      "Text START to 8005550100",
    );

    await userEvent.click(screen.getByRole("button", { name: "Create verification" }));

    await waitFor(() => {
      const call = client.calls.find(
        (candidate) =>
          candidate.path === TFV_PATH && (candidate.init.method ?? "GET") === "POST",
      );
      expect(call).toBeDefined();
      const json = call?.init.json;
      expect(json).not.toHaveProperty("contact_email");
      expect(json).not.toHaveProperty("opt_in_screenshot_url");
      expect(json).not.toHaveProperty("message_volume");
      expect(json).toEqual({
        number_id: "num-tf-1",
        business_name: "Acme Corp",
        use_case: "MIXED",
        use_case_summary: "Weekly promo blasts",
        opt_in_process: "Text START to 8005550100",
      });
    });
  });

  it("offers Submit only on non-terminal rows", async () => {
    const client = makeStubClient(
      routesFor({
        numbers: [makeNumber("num-tf-1", "+18005550100", "tollfree")],
        verifications: ALL_STATUS_ROWS,
      }),
    );
    renderWithProviders(<TollFreeVerificationCard />, client);

    expect(
      await screen.findByRole("button", {
        name: "Submit verification for Draft Biz",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("Approved Biz")).toBeInTheDocument();
    expect(screen.getByText("Rejected Biz")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: /Submit verification for Approved Biz/,
      }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: /Submit verification for Rejected Biz/,
      }),
    ).toBeNull();
  });

  it("shows the rejection reason on a rejected row", async () => {
    const client = makeStubClient(routesFor({ verifications: ALL_STATUS_ROWS }));
    renderWithProviders(<TollFreeVerificationCard />, client);

    expect(
      await screen.findByText(/Reason: Carrier rejected the opt-in proof/),
    ).toBeInTheDocument();
  });

  it("hides the create form and every Submit control without compliance:manage", async () => {
    const client = makeStubClient(
      routesFor({
        permissions: ["compliance:read"],
        numbers: [makeNumber("num-tf-1", "+18005550100", "tollfree")],
        verifications: ALL_STATUS_ROWS,
      }),
    );
    renderWithProviders(<TollFreeVerificationCard />, client);

    // The draft row proves the list itself rendered, so the negatives below are gating and
    // not an empty render.
    expect(await screen.findByText("Draft Biz")).toBeInTheDocument();
    expect(screen.queryByLabelText("Business name")).toBeNull();
    expect(screen.queryByText("New verification")).toBeNull();
    expect(
      screen.queryAllByRole("button", { name: /^Submit verification/ }),
    ).toHaveLength(0);
  });

  it("renders neither the list nor the form without compliance:read", async () => {
    const client = makeStubClient(
      routesFor({
        permissions: [],
        numbers: [makeNumber("num-tf-1", "+18005550100", "tollfree")],
        verifications: ALL_STATUS_ROWS,
      }),
    );
    renderWithProviders(<TollFreeVerificationCard />, client);

    expect(
      await screen.findByText(/do not have permission to view toll-free verifications/i),
    ).toBeInTheDocument();
    // The gate is fail-closed while capabilities load, so the sentence above is not by
    // itself proof that the lookup resolved; the queries having fired is.
    await waitFor(() => {
      expect(client.calls.some((call) => call.path === "/api/v1/me/capabilities")).toBe(
        true,
      );
      expect(client.calls.some((call) => call.path === TFV_PATH)).toBe(true);
    });
    expect(screen.queryByText("Draft Biz")).toBeNull();
    expect(screen.queryByLabelText("Business name")).toBeNull();
    expect(screen.queryByText("New verification")).toBeNull();
  });

  it("submits a draft through the workspace submit endpoint", async () => {
    const client = makeStubClient(routesFor({ verifications: ALL_STATUS_ROWS }));
    renderWithProviders(<TollFreeVerificationCard />, client);

    await userEvent.click(
      await screen.findByRole("button", {
        name: "Submit verification for Draft Biz",
      }),
    );

    await waitFor(() => {
      const call = client.calls.find(
        (candidate) => candidate.path === `${TFV_PATH}/tfv-draft/submit`,
      );
      expect(call).toBeDefined();
      expect(call?.init.method).toBe("POST");
    });

    // The stub only advances the row when it resolved the real id, so the refetched list
    // showing "submitted" (and the mutation's success copy) proves the id round-tripped.
    expect(await screen.findByText("submitted")).toBeInTheDocument();
    expect(
      await screen.findByText("Marked submitted in this workspace."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Submit verification for Draft Biz" }),
    ).toBeInTheDocument();
  });

  it("renders the submit disclosure without claiming anything was sent", async () => {
    const client = makeStubClient(
      routesFor({
        // A local number only: with no eligible toll-free number the create form (and the
        // "consent to receive messages" help text under Opt-in process) is not on screen, so
        // the negative assertions below inspect the disclosure copy itself.
        numbers: [makeNumber("num-local", "+14155550123", "local")],
        verifications: ALL_STATUS_ROWS,
      }),
    );
    renderWithProviders(<TollFreeVerificationCard />, client);

    expect(
      await screen.findByText(/Nothing is transmitted to a carrier from here/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/sent (for|to)/i)).toBeNull();
    expect(screen.queryByText(/submitted to the carrier/i)).toBeNull();
  });

  it("only offers toll-free numbers that carry no verification yet", async () => {
    const client = makeStubClient(
      routesFor({
        numbers: [
          makeNumber("num-free", "+18885550111", "tollfree"),
          makeNumber("num-claimed", "+18005550100", "tollfree"),
          makeNumber("num-local", "+14155550123", "local"),
        ],
        verifications: [
          {
            id: "tfv-claimed",
            number_id: "num-claimed",
            business_name: "Claimed Co",
            status: "draft",
            last_error: null,
          },
        ],
      }),
    );
    renderWithProviders(<TollFreeVerificationCard />, client);

    expect(
      await screen.findByRole("option", { name: "+18885550111" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "+18005550100" })).toBeNull();
    expect(screen.queryByRole("option", { name: "+14155550123" })).toBeNull();
  });

  it("explains when the workspace has no eligible toll-free number", async () => {
    const client = makeStubClient(
      routesFor({
        numbers: [makeNumber("num-local", "+14155550123", "local")],
        verifications: [],
      }),
    );
    renderWithProviders(<TollFreeVerificationCard />, client);

    expect(await screen.findByText("No toll-free numbers available")).toBeInTheDocument();
    expect(screen.queryByLabelText("Business name")).toBeNull();
    expect(screen.queryByRole("button", { name: "Create verification" })).toBeNull();
  });

  it("shows an error and a Retry control when the list fails to load", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": ME_BUSINESS,
      "/api/v1/me/capabilities": {
        permissions: COMPLIANCE_MANAGE,
        org: CAPABILITIES_ORG,
      },
      "/api/v1/numbers": [],
      [TFV_PATH]: () => new Error("boom"),
    });
    renderWithProviders(<TollFreeVerificationCard />, client);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/boom/)).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
