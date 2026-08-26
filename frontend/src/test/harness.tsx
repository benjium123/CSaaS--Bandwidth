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
      const key = Object.keys(routes).find((k) => path.startsWith(k));
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
