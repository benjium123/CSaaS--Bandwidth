import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TenDlcRegistration } from "./TenDlcRegistration";
import type { BrandInput, RegistrationBrand } from "@/api/registration";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

/** Every case in this file runs as a manager; the permission cases live in the sibling file. */
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

interface StubOptions {
  brands?: RegistrationBrand[];
  permissions?: string[];
}

/**
 * One stub per collection. The harness matches by LONGEST prefix, so the
 * "/api/v1/registration/brands" key also answers "/api/v1/registration/brands/{id}/submit";
 * the single handler branches on the method and on the trailing "/submit" rather than
 * relying on a second, shorter key that could shadow the longer path.
 */
function stubRoutes({ brands = [], permissions = MANAGER_PERMISSIONS }: StubOptions = {}) {
  return {
    // The AuthProvider fetches this itself; without a stub it throws "No stub for ...".
    // A business owner in org-1: the panel waits on a membership before it renders.
    "/api/v1/auth/me": {
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
    },
    "/api/v1/registration/texting": {
      registration: null,
      quotes: {
        standard: { fee_tier: "standard", monthly_cents: 1000, upfront_months: 3, due_today_cents: 5450 },
        sole_proprietor: {
          fee_tier: "sole_proprietor",
          monthly_cents: 200,
          upfront_months: 3,
          due_today_cents: 3050,
        },
      },
    },
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
    // CampaignsPanel mounts next to BrandsPanel and always asks for the collection.
    "/api/v1/registration/campaigns": [],
  };
}

describe("TenDlcRegistration brands", () => {
  it("renders the brand list with its status", async () => {
    const client = makeStubClient(
      stubRoutes({ brands: [brand({ name: "Acme Inc", status: "draft" })] }),
    );
    renderWithProviders(<TenDlcRegistration />, client);

    expect(await screen.findByText("Acme Inc")).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
  });

  it("shows the server's missing-field list as words, before any submit attempt", async () => {
    const client = makeStubClient(
      stubRoutes({
        brands: [brand({ missing_for_submission: ["email", "postal_code", "ein"] })],
      }),
    );
    renderWithProviders(<TenDlcRegistration />, client);

    expect(await screen.findByText("Contact email")).toBeInTheDocument();
    expect(screen.getByText("Postal code")).toBeInTheDocument();
    expect(screen.getByText("EIN")).toBeInTheDocument();

    // The warning comes from the server-computed list, so the row is already unsubmittable
    // before anyone clicks anything.
    expect(
      screen.getByRole("button", { name: "Mark brand ready to file: Acme Inc" }),
    ).toBeDisabled();
  });

  it("the create-brand form renders its fields", async () => {
    const client = makeStubClient(stubRoutes());
    renderWithProviders(<TenDlcRegistration />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Add brand" }));

    expect(screen.getByRole("form", { name: "Add brand" })).toBeInTheDocument();
    expect(screen.getByLabelText("Business name")).toBeInTheDocument();
    expect(screen.getByLabelText("EIN")).toBeInTheDocument();
    expect(screen.getByLabelText("Contact email")).toBeInTheDocument();
    expect(screen.getByLabelText("Postal code")).toBeInTheDocument();
    expect(screen.getByLabelText("Entity type")).toBeInTheDocument();
  });

  it("creating a brand posts exactly the expected payload", async () => {
    const client = makeStubClient(stubRoutes());
    renderWithProviders(<TenDlcRegistration />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Add brand" }));

    await userEvent.type(screen.getByLabelText("Business name"), "Acme Inc");
    await userEvent.type(screen.getByLabelText("EIN"), "12-3456789");
    // Deliberately padded: the body builder trims, and the assertion below pins that.
    await userEvent.type(screen.getByLabelText("Contact email"), "  ops@acme.test  ");

    await userEvent.click(screen.getByRole("button", { name: "Create brand" }));

    await waitFor(() => {
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/registration/brands" && call.init.method === "POST",
        ),
      ).toBe(true);
    });

    const post = client.calls.find(
      (call) => call.path === "/api/v1/registration/brands" && call.init.method === "POST",
    );
    if (!post) throw new Error("no POST /api/v1/registration/brands was recorded");

    // toEqual, not toMatchObject: the untouched optional fields must be ABSENT from the
    // body rather than present as "", which toMatchObject would happily accept.
    expect(post.init.json).toEqual({
      name: "Acme Inc",
      entity_type: "PRIVATE_PROFIT",
      country: "US",
      ein: "12-3456789",
      email: "ops@acme.test",
    });
  });

  it("no submit control on a terminal brand: approved", async () => {
    const client = makeStubClient(stubRoutes({ brands: [brand({ status: "approved" })] }));
    renderWithProviders(<TenDlcRegistration />, client);

    // Await the row first: a queryBy* null check against an unrendered tree proves nothing.
    expect(await screen.findByText("Acme Inc")).toBeInTheDocument();

    expect(screen.queryByRole("button", { name: /Mark brand ready to file/ })).toBeNull();
  });

  it("no submit control on a terminal brand: rejected", async () => {
    const client = makeStubClient(
      stubRoutes({
        brands: [brand({ status: "rejected", last_error: "EIN did not match IRS records" })],
      }),
    );
    renderWithProviders(<TenDlcRegistration />, client);

    // Await the row first: a queryBy* null check against an unrendered tree proves nothing.
    expect(await screen.findByText("Acme Inc")).toBeInTheDocument();

    expect(screen.queryByRole("button", { name: /Mark brand ready to file/ })).toBeNull();

    // The reason stays visible even though the row can never be submitted again. The alert
    // renders "Reason: <last_error>", so match on the substring rather than the whole node.
    const reason = screen.getByText(/EIN did not match IRS records/);
    expect(reason).toBeInTheDocument();
    expect(reason).toHaveTextContent("EIN did not match IRS records");
  });

  it("submitting a brand posts to the submit path with no body", async () => {
    const client = makeStubClient(
      stubRoutes({ brands: [brand({ status: "draft", missing_for_submission: [] })] }),
    );
    renderWithProviders(<TenDlcRegistration />, client);

    await userEvent.click(
      await screen.findByRole("button", { name: "Mark brand ready to file: Acme Inc" }),
    );

    await waitFor(() => {
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/registration/brands/brand-1/submit" &&
            call.init.method === "POST",
        ),
      ).toBe(true);
    });

    const submit = client.calls.find(
      (call) =>
        call.path === "/api/v1/registration/brands/brand-1/submit" &&
        call.init.method === "POST",
    );
    if (!submit) throw new Error("no brand submit POST was recorded");
    // The endpoint takes no payload: it validates the stored row and flips the status.
    expect(submit.init.json).toBeUndefined();

    expect(
      await screen.findByText(
        "Brand marked submitted in this workspace. Nothing has been sent to a carrier yet.",
      ),
    ).toBeInTheDocument();
  });
});
