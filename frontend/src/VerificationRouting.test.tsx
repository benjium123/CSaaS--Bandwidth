import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";

vi.mock("@/auth/AuthContext", () => ({ useAuth: () => ({ ready: true, orgId: "o1", me: { id: "u1", memberships: [{ org_id: "o1" }], second_factor_required: false } }) }));
vi.mock("@/api/capabilities", () => ({
  CAPABILITIES_QUERY_KEY: ["me", "capabilities"],
  isOnboardingStep: (s: string) => s === "verification",
  useGate: () => ({ isLoading: false, org: { onboarding_step: "verification" } }),
}));
vi.mock("@/pages/VerificationPage", () => ({ VerificationPage: () => <div>Standalone verification form</div> }));
vi.mock("@/components/shell/Sidebar", () => ({ Sidebar: () => <nav>Sidebar</nav>, MobileTabBar: () => <nav>Mobile navigation</nav> }));
function Location() { const l = useLocation(); return <output>{l.pathname}{l.hash}</output>; }

describe("Verification routing", () => {
  it.each(["/onboarding", "/inbox", "/settings/verification#use_case", "/verification"])("shows only standalone verification from %s", async path => {
    render(<QueryClientProvider client={new QueryClient()}><MemoryRouter initialEntries={[path]}><App /><Location /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText("Standalone verification form")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    expect(screen.getByRole("status").textContent).toBe(`/verification${path.includes("#") ? "#use_case" : ""}`);
  });
});
