import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { useAppointments, useUpdateAppointment, type AppointmentOut } from "@/api/hooks";
import { Badge, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

const STATUS_OPTIONS = ["booked", "canceled", "done"];

const STATUS_FILTERS = [
  { key: "", label: "All" },
  { key: "booked", label: "Booked" },
  { key: "canceled", label: "Canceled" },
  { key: "done", label: "Done" },
];

function statusBadgeClass(status: string): string {
  switch (status) {
    case "done":
      return "bg-green-100 text-green-800";
    case "canceled":
      return "bg-gray-100 text-gray-600";
    default:
      return "bg-amber-100 text-amber-800";
  }
}

function formatParsed(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

function AppointmentRow({ appt }: { appt: AppointmentOut }) {
  const { api } = useAuth();
  const updateAppointment = useUpdateAppointment(api);
  const [error, setError] = React.useState<string | null>(null);

  async function changeStatus(status: string) {
    setError(null);
    try {
      await updateAppointment.mutateAsync({ id: appt.id, status });
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <tr className="align-top">
      <td className="px-3 py-2">{formatPhone(appt.contact_e164)}</td>
      <td className="px-3 py-2">
        <div>{appt.raw_when}</div>
        <div className="text-xs text-muted-foreground">{formatParsed(appt.scheduled_for)}</div>
      </td>
      <td className="max-w-xs px-3 py-2 text-sm text-muted-foreground">{appt.notes || "—"}</td>
      <td className="px-2 py-2">
        <select
          aria-label={`Status for appointment with ${appt.contact_e164}`}
          className="h-8 rounded-md border border-border bg-background px-2 text-xs"
          value={appt.status}
          onChange={(e) => changeStatus(e.target.value)}
          disabled={updateAppointment.isPending}
        >
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <Badge className={`ml-2 ${statusBadgeClass(appt.status)}`}>{appt.status}</Badge>
        {error && (
          <p role="alert" className="mt-1 text-xs text-destructive">
            {error}
          </p>
        )}
      </td>
      <td className="px-2 py-2">
        {appt.created_by === "ai" ? (
          <Badge className="bg-blue-100 text-blue-800">AI</Badge>
        ) : (
          <span className="text-xs text-muted-foreground">{appt.created_by}</span>
        )}
      </td>
    </tr>
  );
}

export function AppointmentsPage() {
  const { api } = useAuth();
  const [status, setStatus] = React.useState("");
  const { data: appointments, isLoading, error } = useAppointments(api, status || undefined);

  return (
    <div className="flex h-full min-h-0 flex-col p-6">
      <div className="mb-4 flex items-center justify-between gap-4">
        <h1 className="text-lg font-semibold">Appointments</h1>
        <select
          aria-label="Filter by status"
          className="h-8 rounded-md border border-border bg-background px-2 text-xs"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          {STATUS_FILTERS.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
            </option>
          ))}
        </select>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <Spinner label="Loading appointments" />
        ) : error ? (
          <p role="alert" className="text-sm text-destructive">
            {(error as Error).message}
          </p>
        ) : (appointments ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground">No appointments yet.</p>
        ) : (
          <table className="w-full text-sm" aria-label="Appointments">
            <thead>
              <tr className="text-left text-xs text-muted-foreground">
                <th className="px-3 py-2 font-medium">Contact</th>
                <th className="px-3 py-2 font-medium">When</th>
                <th className="px-3 py-2 font-medium">Notes</th>
                <th className="px-2 py-2 font-medium">Status</th>
                <th className="px-2 py-2 font-medium">Booked by</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(appointments ?? []).map((appt) => (
                <AppointmentRow key={appt.id} appt={appt} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
