import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { makeStubClient } from "@/test/harness";
import { AuthProvider, useAuth, type Me } from "./AuthContext";

const ME: Me = { id: "u1", email: "a@example.com", full_name: "A", memberships: [] };

/** A deploy restart: the API answers 502 for a moment while the session is still valid. */
const restarting = () => new ApiError(502, "bad_gateway", "The server is restarting.");

function Probe() {
  const { me, ready, refreshMe } = useAuth();
  return (
    <div>
      <p data-testid="state">{!ready ? "starting" : me ? `signed in as ${me.email}` : "signed out"}</p>
      <button type="button" onClick={() => void refreshMe()}>
        Refresh
      </button>
    </div>
  );
}

function renderAuth(client: ReturnType<typeof makeStubClient>) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider client={client}>
        <Probe />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("a restart or a blip never signs anyone out", () => {
  afterEach(() => vi.useRealTimers());

  it("first load keeps asking through a 502 and lands signed in", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let calls = 0;
    const client = makeStubClient({
      "/api/v1/auth/me": () => (++calls <= 2 ? restarting() : ME),
    });
    renderAuth(client);
    expect(screen.getByTestId("state")).toHaveTextContent("starting");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(screen.getByTestId("state")).toHaveTextContent("signed in as a@example.com");
    expect(calls).toBe(3);
  });

  it("a network drop on refresh while signed in keeps the person signed in", async () => {
    let fail = false;
    const client = makeStubClient({
      "/api/v1/auth/me": () => (fail ? new TypeError("Failed to fetch") : ME),
    });
    renderAuth(client);
    expect(await screen.findByText("signed in as a@example.com")).toBeInTheDocument();
    fail = true;
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(screen.getByTestId("state")).toHaveTextContent("signed in as a@example.com");
  });

  it("a real 401 still signs out", async () => {
    let expired = false;
    const client = makeStubClient({
      "/api/v1/auth/me": () =>
        expired ? new ApiError(401, "unauthenticated", "Sign in again") : ME,
    });
    renderAuth(client);
    expect(await screen.findByText("signed in as a@example.com")).toBeInTheDocument();
    expired = true;
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("signed out")).toBeInTheDocument();
  });

  it("gives up and shows sign-in if the API stays down past the retries", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const client = makeStubClient({ "/api/v1/auth/me": () => restarting() });
    renderAuth(client);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(screen.getByTestId("state")).toHaveTextContent("signed out");
  });
});
