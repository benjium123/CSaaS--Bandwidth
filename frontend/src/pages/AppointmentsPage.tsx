import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { useAppointments, useUpdateAppointment, type AppointmentOut } from "@/api/hooks";
import {
  Card,
  EmptyState,
  Pill,
  Section,
  Select,
  Spinner,
  type PillTone,
} from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

const STATUS_OPTIONS = ["booked", "canceled", "done"];

const STATUS_FILTERS = [
  { key: "", label: "All" },
  { key: "booked", label: "Booked" },
  { key: "canceled", label: "Canceled" },
  { key: "done", label: "Done" },
];

function statusTone(status: string): PillTone {
  switch (status) {
    case "done":
      return "success";
    case "canceled":
      return "neutral";
    default:
      return "warning";
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
        <Select
          aria-label={`Status for appointment with ${appt.contact_e164}`}
          className="h-8 px-2 text-xs"
          value={appt.status}
          onChange={(e) => changeStatus(e.target.value)}
          disabled={updateAppointment.isPending}
        >
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </Select>
        <Pill className="ml-2" tone={statusTone(appt.status)}>{appt.status}</Pill>
        {error && (
          <p role="alert" className="mt-1 text-xs text-destructive">
            {error}
          </p>
        )}
      </td>
      <td className="px-2 py-2">
        {appt.created_by === "ai" ? (
          <Pill tone="info">AI</Pill>
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
      <Section
        title="Appointments"
        actions={
          <Select
            aria-label="Filter by status"
            className="h-8 px-2 text-xs"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            {STATUS_FILTERS.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </Select>
        }
        className="flex min-h-0 flex-1 flex-col"
      >
        <div className="min-h-0 flex-1 overflow-y-auto">
          {isLoading ? (
            <Spinner label="Loading appointments" />
          ) : error ? (
            <p role="alert" className="text-sm text-destructive">
              {(error as Error).message}
            </p>
          ) : (appointments ?? []).length === 0 ? (
            <EmptyState title="No appointments yet." />
          ) : (
            <Card className="p-0">
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
            </Card>
          )}
        </div>
      </Section>
    </div>
  );
}
