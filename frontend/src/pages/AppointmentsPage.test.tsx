import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppointmentsPage } from "./AppointmentsPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

function makeAppointment(overrides: Record<string, unknown> = {}) {
  return {
    id: "a1",
    call_id: "call-1",
    contact_e164: "+19725550199",
    raw_when: "tomorrow at 3pm",
    scheduled_for: "2026-09-01T15:00:00+00:00",
    notes: "wants a callback",
    status: "booked",
    created_by: "ai",
    ...overrides,
  };
}

describe("AppointmentsPage", () => {
  it("renders the appointment table and patches status via the select", async () => {
    let appointments = [makeAppointment()];

    const client = makeStubClient({
      "/api/v1/appointments": (path: string, init: RequestInit & { json?: unknown }) => {
        const method = init.method ?? "GET";
        const idMatch = path.match(/^\/api\/v1\/appointments\/([^/?]+)$/);
        if (idMatch && method === "PATCH") {
          const id = idMatch[1];
          const body = init.json as Record<string, unknown>;
          appointments = appointments.map((a) => (a.id === id ? { ...a, ...body } : a));
          return appointments.find((a) => a.id === id);
        }
        return appointments;
      },
    });

    renderWithProviders(<AppointmentsPage />, client);

    expect(await screen.findByText("(972) 555-0199")).toBeInTheDocument();
    expect(screen.getByText("tomorrow at 3pm")).toBeInTheDocument();
    expect(screen.getByText("wants a callback")).toBeInTheDocument();
    expect(screen.getByText("AI")).toBeInTheDocument();

    const select = screen.getByLabelText("Status for appointment with +19725550199");
    await userEvent.selectOptions(select, "done");

    await waitFor(() =>
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/appointments/a1" && c.init.method === "PATCH",
        ),
      ).toBe(true),
    );
    const patchCall = client.calls.find(
      (c) => c.path === "/api/v1/appointments/a1" && c.init.method === "PATCH",
    );
    expect(patchCall?.init.json).toEqual({ status: "done" });
  });

  it("shows an empty state with no appointments", async () => {
    const client = makeStubClient({ "/api/v1/appointments": [] });
    renderWithProviders(<AppointmentsPage />, client);
    expect(await screen.findByText("No appointments yet.")).toBeInTheDocument();
  });
});
