import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OrgFeaturesPanel, type OrgFeature } from "./OrgFeaturesPanel";

const mocks = vi.hoisted(() => ({ requestMock: vi.fn() }));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ api: { request: mocks.requestMock } }),
}));

const ORG_ID = "o1";
const BASE = `/api/v1/ops/console/orgs/${ORG_ID}/features`;

type RequestInitLike = { method?: string; json?: unknown };

function methodOf(init?: RequestInitLike): string {
  return init?.method ?? "GET";
}

function renderPanel(canEdit = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <OrgFeaturesPanel orgId={ORG_ID} canEdit={canEdit} />
    </QueryClientProvider>,
  );
}

function feature(
  overrides: Partial<OrgFeature> & Pick<OrgFeature, "key" | "label" | "group">,
): OrgFeature {
  return {
    description: "",
    default_enabled: false,
    enabled: false,
    price_metric: null,
    ...overrides,
  };
}

function callRecording(): OrgFeature {
  return feature({
    key: "call_recording",
    label: "Call recording",
    group: "Calling",
    description: "Record inbound and outbound calls.",
    price_metric: "recording_min",
  });
}

function switchFor(label: string): HTMLInputElement {
  return screen.getByRole("switch", { name: label }) as HTMLInputElement;
}

beforeEach(() => {
  mocks.requestMock.mockReset();
});

describe("OrgFeaturesPanel", () => {
  it("lists features by group and flags the ones that differ from their default", async () => {
    const features: OrgFeature[] = [
      callRecording(),
      feature({
        key: "voicemail",
        label: "Voicemail",
        group: "Calling",
        description: "Take a message when nobody picks up.",
        default_enabled: true,
        enabled: false,
      }),
      feature({
        key: "sms_outbound",
        label: "Outbound SMS",
        group: "Messaging",
        description: "Send text messages.",
        default_enabled: true,
        enabled: true,
        price_metric: "sms_out",
      }),
    ];

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") return { org_id: ORG_ID, features };
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel();

    expect(await screen.findByText("Call recording")).toBeTruthy();
    expect(screen.getByText("Voicemail")).toBeTruthy();
    expect(screen.getByText("Outbound SMS")).toBeTruthy();

    const calling = screen.getByText("Calling");
    const messaging = screen.getByText("Messaging");
    expect(
      calling.compareDocumentPosition(messaging) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // Only the feature whose live value differs from its default is flagged as changed.
    expect(screen.getByText("Changed")).toBeTruthy();
    expect(screen.getByText("Default off")).toBeTruthy();
    expect(screen.getByText("Default on")).toBeTruthy();

    expect(screen.getByText("Billed as recording_min")).toBeTruthy();
    expect(screen.getByText("No separate charge")).toBeTruthy();

    expect((switchFor("Call recording") as HTMLInputElement).checked).toBe(false);
    expect((switchFor("Outbound SMS") as HTMLInputElement).checked).toBe(true);
  });

  it("PUTs the new value for the feature when its switch is toggled", async () => {
    const user = userEvent.setup();
    let putPath: string | null = null;
    let putBody: unknown = null;

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { org_id: ORG_ID, features: [callRecording()] };
      }
      if (path === `${BASE}/call_recording` && method === "PUT") {
        putPath = path;
        putBody = init?.json ?? null;
        return { org_id: ORG_ID, key: "call_recording", enabled: true };
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    const recorder = await screen.findByRole("switch", { name: "Call recording" });
    expect((recorder as HTMLInputElement).checked).toBe(false);

    await user.click(recorder);

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putPath).toBe(`${BASE}/call_recording`);
    expect(putBody).toEqual({ enabled: true });
  });

  it("disables the switches and explains why when the operator cannot edit", async () => {
    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      if (path === BASE && methodOf(init) === "GET") {
        return { org_id: ORG_ID, features: [callRecording()] };
      }
      throw new Error(`unexpected request ${methodOf(init)} ${path}`);
    });

    renderPanel(false);

    await screen.findByRole("switch", { name: "Call recording" });
    expect(
      screen.getByText("Read-only: only operator admins can change features."),
    ).toBeTruthy();
    expect(switchFor("Call recording").disabled).toBe(true);
  });

  it("shows the server error message when a toggle is rejected", async () => {
    const user = userEvent.setup();

    mocks.requestMock.mockImplementation(async (path: string, init?: RequestInitLike) => {
      const method = methodOf(init);
      if (path === BASE && method === "GET") {
        return { org_id: ORG_ID, features: [callRecording()] };
      }
      if (method === "PUT") {
        throw Object.assign(new Error("Forbidden: operator needs the admin role"), {
          status: 403,
        });
      }
      throw new Error(`unexpected request ${method} ${path}`);
    });

    renderPanel();

    await user.click(await screen.findByRole("switch", { name: "Call recording" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent ?? "").toMatch(/forbidden/i);
  });
});
