import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CampaignsPanel } from "./CampaignsPanel";
import type { RegistrationBrand, RegistrationCampaign } from "@/api/registration";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const CAMPAIGNS = "/api/v1/registration/campaigns";

const BRAND: RegistrationBrand = {
  id: "brand-1",
  name: "Acme Inc",
  entity_type: "PRIVATE_PROFIT",
  status: "approved",
  carrier_refs: {},
  last_error: null,
  missing_for_submission: [],
};

function campaign(overrides: Partial<RegistrationCampaign> = {}): RegistrationCampaign {
  return {
    id: "camp-1",
    brand_id: "brand-1",
    name: "Owner outreach",
    use_case: "MIXED",
    status: "draft",
    carrier_refs: {},
    last_error: null,
    number_count: 0,
    missing_for_submission: [],
    description: "Texts to land owners",
    sample_messages: ["Hi from Acme. Reply STOP to opt out."],
    privacy_policy_url: "https://acme.test/privacy",
    opt_out_keywords: "STOP",
    expected_monthly_messages: 500,
    ...overrides,
  };
}

function routes(list: RegistrationCampaign[]) {
  return {
    "/api/v1/me/capabilities": {
      permissions: ["compliance:read", "compliance:manage"],
      org: { has_provider: true, has_number: true, member_count: 2, registration_state: "approved" },
    },
    "/api/v1/registration/brands": [BRAND],
    [CAMPAIGNS]: ((_path: string, init: RequestInit & { json?: unknown }) => {
      if ((init.method ?? "GET") === "POST") return campaign();
      return list;
    }) as RouteStub,
    [`${CAMPAIGNS}/camp-1`]: campaign(),
  };
}

describe("CampaignsPanel filing fields", () => {
  it("the add form has the new filing fields and keyword hints", async () => {
    renderWithProviders(<CampaignsPanel />, makeStubClient(routes([])));
    await userEvent.click(await screen.findByRole("button", { name: "Add campaign" }));

    for (const label of [
      "Privacy policy URL",
      "Terms URL",
      "Opt-in keywords",
      "Opt-out keywords",
      "Help keywords",
      "Opt-in confirmation message",
      "Opt-in proof (link to a screenshot or your sign-up page)",
      "Expected messages per month",
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
    expect(screen.getAllByText("Comma separated, e.g. STOP, UNSUBSCRIBE")).toHaveLength(3);
  });

  it("creating sends the new fields, and leaves untouched ones out", async () => {
    const client = makeStubClient(routes([]));
    renderWithProviders(<CampaignsPanel />, client);
    await userEvent.click(await screen.findByRole("button", { name: "Add campaign" }));

    await userEvent.selectOptions(await screen.findByLabelText("Brand"), "brand-1");
    await userEvent.type(screen.getByLabelText("Campaign name"), "Owner outreach");
    await userEvent.type(screen.getByLabelText("Privacy policy URL"), "https://acme.test/p");
    await userEvent.type(screen.getByLabelText("Opt-out keywords"), "stop, end");
    await userEvent.type(screen.getByLabelText("Expected messages per month"), "250");
    await userEvent.click(screen.getByRole("button", { name: "Create campaign" }));

    await waitFor(() => {
      expect(client.calls.some((c) => c.path === CAMPAIGNS && c.init.method === "POST")).toBe(true);
    });
    const post = client.calls.find((c) => c.path === CAMPAIGNS && c.init.method === "POST");
    expect(post?.init.json).toEqual({
      brand_id: "brand-1",
      name: "Owner outreach",
      use_case: "MIXED",
      privacy_policy_url: "https://acme.test/p",
      opt_out_keywords: "stop, end",
      sample_messages: [],
      expected_monthly_messages: 250,
    });
  });

  it("a draft or declined campaign gets Edit, pre-filled, saved with PATCH", async () => {
    const client = makeStubClient(routes([campaign()]));
    renderWithProviders(<CampaignsPanel />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Edit: Owner outreach" }));
    expect(screen.getByRole("form", { name: "Edit campaign" })).toBeInTheDocument();
    expect(screen.getByLabelText("Privacy policy URL")).toHaveValue("https://acme.test/privacy");
    expect(screen.getByLabelText("Expected messages per month")).toHaveValue(500);
    expect(screen.getByLabelText("Brand")).toBeDisabled();

    const terms = screen.getByLabelText("Terms URL");
    await userEvent.type(terms, "https://acme.test/terms");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(client.calls.some((c) => c.init.method === "PATCH")).toBe(true);
    });
    const patch = client.calls.find((c) => c.init.method === "PATCH");
    expect(patch?.path).toBe(`${CAMPAIGNS}/camp-1`);
    const body = patch?.init.json as Record<string, unknown>;
    expect(body.brand_id).toBeUndefined();
    expect(body.terms_url).toBe("https://acme.test/terms");
    expect(body.help_keywords).toBeNull(); // blank clears
    expect(body.expected_monthly_messages).toBe(500);
  });

  it("no Edit button on a campaign that is past draft", async () => {
    renderWithProviders(
      <CampaignsPanel />,
      makeStubClient(routes([campaign({ status: "approved" })])),
    );
    expect(await screen.findByText("Owner outreach")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Edit:/ })).toBeNull();
  });

  it("a declined campaign can be edited", async () => {
    renderWithProviders(
      <CampaignsPanel />,
      makeStubClient(routes([campaign({ status: "rejected" })])),
    );
    expect(await screen.findByRole("button", { name: "Edit: Owner outreach" })).toBeInTheDocument();
  });
});
