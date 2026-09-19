import * as React from "react";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ApiClient, AuthState } from "@/api/client";
import { AuthProvider } from "@/auth/AuthContext";

/**
 * A stubbed ApiClient. No MSW: one less dependency, and route stubs stay explicit and
 * fully deterministic.
 */
export type RouteStub = (path: string, init: RequestInit & { json?: unknown }) => unknown;

export function makeStubClient(routes: Record<string, RouteStub | unknown>): ApiClient & {
  calls: { path: string; init: RequestInit & { json?: unknown } }[];
} {
  const auth: AuthState = { token: "test-token", orgId: "org-1" };
  const calls: { path: string; init: RequestInit & { json?: unknown } }[] = [];

  const client = {
    auth,
    calls,
    setAuth(next: Partial<AuthState>) {
      Object.assign(client.auth, next);
    },
    async request<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
      calls.push({ path, init });
      // LONGEST prefix wins, not the first one declared. With `find`, a stub for
      // "/api/v1/orgs/current" silently swallowed "/api/v1/orgs/current/members" and handed
      // the members query the org OBJECT where the server sends an ARRAY - so the page threw
      // `.map is not a function`, the error boundary ate it, and the suite stayed green while
      // rendering a crash instead of the page. A shorter key shadowing a longer path is
      // invisible by construction: nothing fails, the stub just answers the wrong question.
      const key = Object.keys(routes)
        .filter((k) => path.startsWith(k))
        .sort((a, b) => b.length - a.length)[0];
      if (!key) throw new Error(`No stub for ${path}`);
      const handler = routes[key];
      const value = typeof handler === "function" ? (handler as RouteStub)(path, init) : handler;
      if (value instanceof Error) throw value;
      return value as T;
    },
  } as ApiClient & { calls: typeof calls };

  return client;
}

export function renderWithProviders(ui: React.ReactNode, client: ApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}
