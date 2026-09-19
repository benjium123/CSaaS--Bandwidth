import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TenDlcRegistration } from "./TenDlcRegistration";
import type {
  BrandInput,
  CampaignInput,
  RegistrationBrand,
  RegistrationCampaign,
} from "@/api/registration";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

/** The write cases run as a manager; the read-only and no-permission cases override this. */
const MANAGER_PERMISSIONS = ["compliance:read", "compliance:manage"];

function capabilities(permissions: string[]) {
  return {
    permissions,
    org: {
      has_provider: true,
      has_number: true,
      member_count: 2,
      registration_state: "approved",
    },
  };
}

function brand(overrides: Partial<RegistrationBrand> = {}): RegistrationBrand {
  return {
    id: "brand-1",
    name: "Acme Inc",
    entity_type: "PRIVATE_PROFIT",
    status: "draft",
    carrier_refs: {},
    last_error: null,
    missing_for_submission: [],
    ...overrides,
  };
}

function campaign(overrides: Partial<RegistrationCampaign> = {}): RegistrationCampaign {
  return {
    id: "camp-1",
    brand_id: "brand-1",
    name: "Acme Alerts",
    use_case: "MIXED",
    status: "draft",
    carrier_refs: {},
    last_error: null,
    number_count: 0,
    missing_for_submission: [],
    ...overrides,
  };
}

interface StubOptions {
  brands?: RegistrationBrand[];
  campaigns?: RegistrationCampaign[];
  permissions?: string[];
}

/**
 * One stub per collection. The harness matches by LONGEST prefix, so the
 * "/api/v1/registration/brands" and "/api/v1/registration/campaigns" keys also answer their
 * own "/{id}/submit" children; each handler branches on the method and on the trailing
 * "/submit" rather than relying on a second, shorter key that could shadow the longer path.
 */
function stubRoutes({
  brands = [],
  campaigns = [],
  permissions = MANAGER_PERMISSIONS,
}: StubOptions = {}) {
  return {
    // The AuthProvider fetches this itself; without a stub it throws "No stub for ...".
    "/api/v1/auth/me": { id: "u-1", email: "u@example.com", permissions: [] },
    "/api/v1/me/capabilities": capabilities(permissions),
    "/api/v1/registration/brands": ((path: string, init: RequestInit & { json?: unknown }) => {
      if ((init.method ?? "GET") === "POST") {
        if (path.endsWith("/submit")) {
          return brand({ status: "submitted" });
        }
        const input = (init.json ?? {}) as BrandInput;
        return brand({ name: input.name });
      }
      return brands;
    }) as RouteStub,
    "/api/v1/registration/campaigns": ((
      path: string,
      init: RequestInit & { json?: unknown },
    ) => {
      if ((init.method ?? "GET") === "POST") {
        if (path.endsWith("/submit")) {
          return campaign({ status: "submitted" });
        }
        const input = (init.json ?? {}) as CampaignInput;
        return campaign({ name: input.name });
      }
      return campaigns;
    }) as RouteStub,
  };
}

describe("TenDlcRegistration access and campaigns", () => {
  it("a read-only user sees the state and none of the write controls", async () => {
    const client = makeStubClient(
      stubRoutes({
        permissions: ["compliance:read"],
        brands: [brand({ missing_for_submission: ["email"] })],
        campaigns: [campaign()],
      }),
    );
    renderWithProviders(<TenDlcRegistration />, client);

    // These two come first and are awaited: they are what stop the four null checks below
    // from passing only because nothing rendered at all. Both rows are awaited - the brand
    // row and the campaign row - so each null check runs against a settled list.
    expect(await screen.findByText("Acme Inc")).toBeInTheDocument();
    expect(await screen.findByText("Acme Alerts")).toBeInTheDocument();
    expect(screen.getByText("Contact email")).toBeInTheDocument();

    expect(screen.queryByRole("button", { name: "Add brand" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add campaign" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Mark brand ready to file/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Mark campaign ready to file/ })).toBeNull();

    expect(client.calls.every((call) => (call.init.method ?? "GET") === "GET")).toBe(true);
  });

  it("a manager sees the write controls the reader does not", async () => {
    const client = makeStubClient(
      stubRoutes({
        permissions: MANAGER_PERMISSIONS,
        brands: [brand({ missing_for_submission: ["email"] })],
        campaigns: [campaign()],
      }),
    );
    renderWithProviders(<TenDlcRegistration />, client);

    // The same fixtures as the read-only case: the only thing that changed is the grant.
    // Await both rows first - the row-level submit buttons depend on the brands and
    // campaigns queries, which answer after the capabilities query that gates "Add ...".
    expect(await screen.findByText("Acme Inc")).toBeInTheDocument();
    expect(await screen.findByText("Acme Alerts")).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Add brand" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add campaign" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Mark brand ready to file/ })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Mark campaign ready to file/ }),
    ).toBeInTheDocument();
  });

  it("a user with neither permission sees nothing and issues no registration request", async () => {
    const client = makeStubClient(stubRoutes({ permissions: [] }));
    renderWithProviders(<TenDlcRegistration />, client);

    expect(
      await screen.findByText(
        "You do not have access to 10DLC registration for this workspace.",
      ),
    ).toBeInTheDocument();

    // Checked after the access text has rendered, so this cannot pass merely because no
    // request has been made yet.
    await waitFor(() => {
      expect(client.calls.some((call) => call.path.startsWith("/api/v1/registration"))).toBe(
        false,
      );
    });
  });

  it("a campaign cannot be submitted while its brand is not approved", async () => {
    const client = makeStubClient(
      stubRoutes({
        permissions: MANAGER_PERMISSIONS,
        brands: [brand({ id: "brand-1", status: "draft" })],
        campaigns: [
          campaign({ brand_id: "brand-1", status: "draft", missing_for_submission: [] }),
        ],
      }),
    );
    renderWithProviders(<TenDlcRegistration />, client);

    expect(
      await screen.findByRole("button", { name: "Mark campaign ready to file: Acme Alerts" }),
    ).toBeDisabled();
    expect(
      screen.getByText("The brand must be approved before this campaign can be submitted."),
    ).toBeInTheDocument();
  });

  it("a campaign can be submitted once its brand is approved", async () => {
    const client = makeStubClient(
      stubRoutes({
        permissions: MANAGER_PERMISSIONS,
        brands: [brand({ id: "brand-1", status: "approved" })],
        campaigns: [
          campaign({ brand_id: "brand-1", status: "draft", missing_for_submission: [] }),
        ],
      }),
    );
    renderWithProviders(<TenDlcRegistration />, client);

    expect(await screen.findByText("Acme Alerts")).toBeInTheDocument();

    expect(
      screen.getByRole("button", { name: "Mark campaign ready to file: Acme Alerts" }),
    ).toBeEnabled();
    expect(
      screen.queryByText("The brand must be approved before this campaign can be submitted."),
    ).toBeNull();
  });

  it("creating a campaign splits sample messages into an array and drops blank lines", async () => {
    const client = makeStubClient(
      stubRoutes({
        permissions: MANAGER_PERMISSIONS,
        brands: [brand({ id: "brand-1", name: "Acme Inc", status: "approved" })],
        campaigns: [],
      }),
    );
    renderWithProviders(<TenDlcRegistration />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Add campaign" }));

    // The form only renders once the brands query has answered, so wait for its first field
    // before reaching for the labelled controls.
    await screen.findByLabelText("Brand");

    await userEvent.selectOptions(screen.getByLabelText("Brand"), "brand-1");
    await userEvent.type(screen.getByLabelText("Campaign name"), "Acme Alerts");
    // Two content lines with a blank line between and trailing spaces on the last: the blank
    // line and the padding are exactly what `campaignCreateBody` must clean out.
    await userEvent.type(
      screen.getByLabelText("Sample messages"),
      "Your code is 1234{enter}{enter}  Reply STOP to opt out  ",
    );

    await userEvent.click(screen.getByRole("button", { name: "Create campaign" }));

    await waitFor(() => {
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/registration/campaigns" && call.init.method === "POST",
        ),
      ).toBe(true);
    });

    const post = client.calls.find(
      (call) =>
        call.path === "/api/v1/registration/campaigns" && call.init.method === "POST",
    );
    if (!post) throw new Error("no POST /api/v1/registration/campaigns was recorded");

    const body = (post.init.json ?? {}) as {
      brand_id: string;
      name: string;
      sample_messages: string[];
    };
    expect(body.brand_id).toBe("brand-1");
    expect(body.name).toBe("Acme Alerts");
    // The blank middle line is dropped and each remaining line is trimmed.
    expect(body.sample_messages).toEqual(["Your code is 1234", "Reply STOP to opt out"]);
  });
});
