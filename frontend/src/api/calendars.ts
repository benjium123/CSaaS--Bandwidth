import type { ApiClient } from "./client";

/** A connected external calendar (2026-10-07: Google). Tokens never leave the server. */
export interface CalendarConnection {
  id: string;
  provider: "google";
  account_email: string;
  /** "error" = the provider refused our access; reconnect to repair it. */
  status: "active" | "error";
  last_error: string | null;
  connected_by_me: boolean;
  created_at: string;
}

export interface CalendarConnections {
  /** False when the server has no Google OAuth client configured: hide the button. */
  google_available: boolean;
  connections: CalendarConnection[];
}

export const CALENDAR_CONNECTIONS_KEY = ["calendar-connections"] as const;

export async function listCalendarConnections(api: ApiClient): Promise<CalendarConnections> {
  return api.request<CalendarConnections>("/api/v1/calendar/connections");
}

/** Returns Google's consent URL; the caller sends the browser there. */
export async function startGoogleConnect(api: ApiClient): Promise<string> {
  const out = await api.request<{ url: string }>("/api/v1/calendar/oauth/google/start", {
    method: "POST",
  });
  return out.url;
}

export async function deleteCalendarConnection(api: ApiClient, id: string): Promise<void> {
  await api.request<void>(`/api/v1/calendar/connections/${id}`, { method: "DELETE" });
}
