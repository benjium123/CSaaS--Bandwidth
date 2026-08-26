import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NumbersPage } from "./NumbersPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const NUMBER_1 = {
  id: "num-1",
  e164: "+12145550100",
  carrier: "bandwidth",
  is_active: true,
  number_type: "local",
  status: "active",
  capabilities: {},
  campaign_id: null,
  registration: "approved",
  registration_detail: "10DLC campaign verified",
};

const AVAILABLE = [
  {
    e164: "+12145550111",
    number_type: "local",
    region: "TX",
    locality: "Dallas",
    monthly_cost: "1.00",
    capabilities: {},
  },
];

const ORDERED_NUMBER = {
  ...NUMBER_1,
  id: "num-2",
  e164: "+12145550111",
  registration: "unknown",
  registration_detail: "",
};

describe("NumbersPage", () => {
  it("shows the registration badge and orders a number from search results", async () => {
    const client = makeStubClient({
      "/api/v1/numbers/available": AVAILABLE,
      "/api/v1/numbers/order": ORDERED_NUMBER,
      "/api/v1/registration/campaigns": [],
      "/api/v1/numbers": [NUMBER_1],
    });
    renderWithProviders(<NumbersPage />, client);

    expect(await screen.findByText("approved")).toBeInTheDocument();
    expect(screen.getByTitle("10DLC campaign verified")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("(214) 555-0111")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Order" }));

    await waitFor(() =>
      expect(client.calls.some((c) => c.path === "/api/v1/numbers/order")).toBe(true),
    );
    const orderCall = client.calls.find((c) => c.path === "/api/v1/numbers/order");
    expect(orderCall?.init.json).toEqual({ e164: "+12145550111", carrier: undefined });
  });
});
