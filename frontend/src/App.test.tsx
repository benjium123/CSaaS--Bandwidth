import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { AuthProvider, type Me } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";

// Item 12: a crashing routed page must not take the Sidebar/softphone dock down with it.
// The softphone stack pulls in a real WebSocket + livekit-client, which is irrelevant to
// what this test is checking (the Shell-level error boundary), so it's stubbed out.
vi.mock("@/softphone/SoftphoneProvider", () => ({
  SoftphoneProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/softphone/SoftphonePanel", () => ({ SoftphonePanel: () => null }));

vi.mock("@/pages/ConversationsPage", () => ({
  ConversationsPage: () => {
    throw new Error("page crashed");
  },
}));

const ME: Me = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
};

function renderApp() {
  const client = makeStubClient({ "/api/v1/auth/me": ME, "/api/v1/inboxes": [] });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={["/inbox"]}>
          <App />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("App shell error boundary", () => {
  it("keeps the sidebar usable when a routed page throws", async () => {
    // React (and this app's own ErrorBoundary) logs the caught error to the console by
    // design - silence it here so the test output stays clean.
    vi.spyOn(console, "error").mockImplementation(() => undefined);

    renderApp();

    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    // The page crashed, but the persistent nav frame around it did not.
    expect(screen.getByRole("navigation", { name: "Sidebar" })).toBeInTheDocument();
  });
});
