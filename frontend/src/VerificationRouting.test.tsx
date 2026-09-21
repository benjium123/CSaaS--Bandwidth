import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { LifecycleGate } from "@/auth/LifecycleGate";

const state = vi.hoisted(() => ({ step: "verification" }));
beforeEach(() => { state.step = "verification"; });

vi.mock("@/auth/AuthContext", () => ({ useAuth: () => ({ ready: true, orgId: "o1", me: { id: "u1", memberships: [{ org_id: "o1" }], second_factor_required: false } }) }));
vi.mock("@/api/capabilities", () => ({
  CAPABILITIES_QUERY_KEY: ["me", "capabilities"],
  isOnboardingStep: (s: string) => ["verification", "awaiting_review", "numbers", "ready"].includes(s),
  useGate: () => ({ isLoading: false, org: { onboarding_step: state.step } }),
}));
vi.mock("@/pages/VerificationPage", () => ({ VerificationPage: () => <div>Standalone verification form</div> }));
vi.mock("@/components/shell/Sidebar", () => ({ Sidebar: () => <nav>Sidebar</nav>, MobileTabBar: () => <nav>Mobile navigation</nav> }));
function Location() { const l = useLocation(); return <output>{l.pathname}{l.hash}</output>; }

describe("Verification routing", () => {
  it("takes an approved account to number purchasing, then permits team setup after purchase", async () => {
    state.step = "awaiting_review";
    const client = new QueryClient();
    const view = () => <QueryClientProvider client={client}><MemoryRouter initialEntries={["/verification"]}><LifecycleGate><div>Current step</div></LifecycleGate><Location /></MemoryRouter></QueryClientProvider>;
    const result = render(view());
    expect(screen.getByRole("status")).toHaveTextContent("/verification");
    state.step = "numbers";
    result.rerender(view());
    expect(await screen.findByText("/choose-numbers")).toBeInTheDocument();
    result.unmount();
    state.step = "ready";
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={["/settings/team"]}><LifecycleGate><div>Team setup</div></LifecycleGate><Location /></MemoryRouter></QueryClientProvider>);
    expect(screen.getByText("Team setup")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("/settings/team");
  });
  it.each(["/onboarding", "/inbox", "/settings/verification#use_case", "/verification"])("shows only standalone verification from %s", async path => {
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={[path]}><App /><Location /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText("Standalone verification form")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.getByRole("status").textContent).toBe(`/verification${path.includes("#") ? "#use_case" : ""}`);
  });
});
