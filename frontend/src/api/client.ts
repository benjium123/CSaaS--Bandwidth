/**
 * The one place that talks HTTP.
 *
 * Attaches the P0 auth contract (Bearer + X-Org-Id) to every call, and turns any 401 into
 * a single "you are logged out" signal so no page has to handle it individually.
 */

export type AuthState = {
  token: string | null;
  orgId: string | null;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface ApiClient {
  request<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T>;
  auth: AuthState;
  setAuth(next: Partial<AuthState>): void;
  onUnauthorized?: () => void;
}

const STORAGE_KEY = "csaas.auth";

export function loadStoredAuth(): AuthState {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { token: null, orgId: null };
    const parsed = JSON.parse(raw) as AuthState;
    return { token: parsed.token ?? null, orgId: parsed.orgId ?? null };
  } catch {
    return { token: null, orgId: null };
  }
}

export function storeAuth(state: AuthState): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    /* private mode - the session simply will not persist */
  }
}

export function clearStoredAuth(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

export function createClient(baseUrl = ""): ApiClient {
  const client: ApiClient = {
    auth: loadStoredAuth(),

    setAuth(next) {
      client.auth = { ...client.auth, ...next };
      storeAuth(client.auth);
    },

    async request<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
      const headers = new Headers(init.headers);
      if (client.auth.token) headers.set("Authorization", `Bearer ${client.auth.token}`);
      if (client.auth.orgId) headers.set("X-Org-Id", client.auth.orgId);

      let body = init.body;
      if (init.json !== undefined) {
        headers.set("Content-Type", "application/json");
        body = JSON.stringify(init.json);
      }

      const res = await fetch(`${baseUrl}${path}`, { ...init, headers, body });

      if (res.status === 401) {
        clearStoredAuth();
        client.auth = { token: null, orgId: null };
        client.onUnauthorized?.();
      }

      if (res.status === 204) return undefined as T;

      const text = await res.text();
      const payload = text ? JSON.parse(text) : null;

      if (!res.ok) {
        const err = payload?.error;
        throw new ApiError(
          res.status,
          err?.code ?? "http_error",
          err?.message ?? `Request failed with ${res.status}`,
        );
      }
      return payload as T;
    },
  };
  return client;
}
