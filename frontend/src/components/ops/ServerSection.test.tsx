import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ServerSnapshot } from "@/api/opsServer";
import { ServerSection } from "./ServerSection";

const mocks = vi.hoisted(() => ({ requestMock: vi.fn() }));

vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ api: { request: mocks.requestMock } }),
}));

const PATH = "/api/v1/ops/console/server";

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ServerSection />
    </QueryClientProvider>,
  );
}

function snapshot(overrides: Partial<ServerSnapshot> = {}): ServerSnapshot {
  return {
    scope: "host",
    label: "Whole server (shared with other apps)",
    sampled_at: "2024-01-01T00:00:00+00:00",
    cpu: { percent: 20, cores: 4 },
    memory: { total_bytes: 2048, available_bytes: 512, used_bytes: 1536, percent: 75 },
    disk: { total_bytes: 100, used_bytes: 60, free_bytes: 40, percent: 60 },
    load: { one: 0.42, five: 0.3, fifteen: 0.2, cores: 2, percent: 21 },
    uptime_seconds: 274320,
    apps: {
      available: true,
      stale: false,
      updated_at: "2024-01-01T00:00:00+00:00",
      age_seconds: 3,
      containers: [
        {
          name: "csaas-api-1",
          cpu_percent: 1.23,
          mem_used_bytes: 105381888,
          mem_limit_bytes: 2063725000,
          mem_percent: 5.1,
          pids: 12,
        },
        {
          name: "csaas-web-1",
          cpu_percent: 9.5,
          mem_used_bytes: 10485760,
          mem_limit_bytes: 1073741824,
          mem_percent: 1,
          pids: 7,
        },
      ],
    },
    thresholds: { warn: 80, critical: 90 },
    ...overrides,
  };
}

beforeEach(() => {
  mocks.requestMock.mockReset();
});

describe("ServerSection", () => {
  it("renders the label, the four metrics and the app rows", async () => {
    mocks.requestMock.mockImplementation(async (path: string) => {
      if (path === PATH) return snapshot();
      throw new Error(`unexpected request ${path}`);
    });

    renderSection();

    expect(await screen.findByText("Whole server (shared with other apps)")).toBeTruthy();
    expect(screen.getByText("CPU")).toBeTruthy();
    expect(screen.getByText("Memory")).toBeTruthy();
    expect(screen.getByText("Disk")).toBeTruthy();
    expect(screen.getByText("Load")).toBeTruthy();
    expect(screen.getByText("csaas-api-1")).toBeTruthy();
    expect(screen.getByText("csaas-web-1")).toBeTruthy();
  });

  it("colours the gauges by threshold", async () => {
    mocks.requestMock.mockResolvedValue(
      snapshot({
        cpu: { percent: 20, cores: 4 },
        memory: { total_bytes: 2048, available_bytes: 307, used_bytes: 1741, percent: 85 },
        disk: { total_bytes: 100, used_bytes: 95, free_bytes: 5, percent: 95 },
      }),
    );

    renderSection();

    const cpu = (await screen.findByRole("img", { name: "CPU 20%" })).closest("[data-level]");
    expect(cpu?.getAttribute("data-level")).toBe("ok");

    const memory = screen.getByRole("img", { name: "Memory 85%" }).closest("[data-level]");
    expect(memory?.getAttribute("data-level")).toBe("warn");

    const disk = screen.getByRole("img", { name: "Disk 95%" }).closest("[data-level]");
    expect(disk?.getAttribute("data-level")).toBe("critical");
  });

  it("shows the empty state when per-app usage is not available", async () => {
    mocks.requestMock.mockResolvedValue(
      snapshot({
        apps: { available: false, stale: false, updated_at: null, age_seconds: null, containers: [] },
      }),
    );

    renderSection();

    expect(await screen.findByText("Per-app usage isn't set up on this server yet")).toBeTruthy();
  });

  it("shows Not available for a null metric", async () => {
    mocks.requestMock.mockResolvedValue(snapshot({ memory: null }));

    renderSection();

    expect(await screen.findByText("Not available")).toBeTruthy();
  });
});
