import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { useAppointments, useUpdateAppointment, type AppointmentOut } from "@/api/hooks";
import {
  EmptyState,
  Pill,
  Select,
  Spinner,
  type PillTone,
} from "@/components/ui/primitives";
import { InitialsAvatar, PageHeader, SurfaceCard } from "@/components/ui/consoleChrome";
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

  const display = formatPhone(appt.contact_e164);

  return (
    <tr className="align-top">
      <td className="px-[12px] py-[11px]">
        {/* The reference lists a person with their mark: the number is the stable seed, so
            a reformatted display value does not repaint the row. */}
        <div className="flex items-center gap-[11px]">
          <InitialsAvatar name={display} seed={appt.contact_e164} size="sm" />
          <span className="font-medium">{display}</span>
        </div>
      </td>
      <td className="px-[12px] py-[11px]">
        <div>{appt.raw_when}</div>
        <div className="text-[11.5px] text-[hsl(var(--cx-muted))]">{formatParsed(appt.scheduled_for)}</div>
      </td>
      <td className="max-w-xs px-[12px] py-[11px] text-[13px] text-[hsl(var(--cx-subtle))]">
        {appt.notes || "—"}
      </td>
      <td className="px-[12px] py-[11px]">
        <div className="flex flex-wrap items-center gap-[8px]">
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
          <Pill tone={statusTone(appt.status)}>{appt.status}</Pill>
        </div>
        {error && (
          <p role="alert" className="mt-1 text-[11.5px] text-[hsl(var(--cx-danger))]">
            {error}
          </p>
        )}
      </td>
      <td className="px-[12px] py-[11px]">
        {appt.created_by === "ai" ? (
          <Pill tone="info">AI</Pill>
        ) : (
          <span className="text-[11.5px] text-[hsl(var(--cx-muted))]">{appt.created_by}</span>
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
    <div className="flex h-full min-h-0 flex-col gap-[14px]">
      <PageHeader
        title="Appointments"
        headingLevel={2}
        description="What your assistant booked, and where each one stands."
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
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <Spinner label="Loading appointments" />
        ) : error ? (
          <p role="alert" className="text-[13px] text-[hsl(var(--cx-danger))]">
            {(error as Error).message}
          </p>
        ) : (appointments ?? []).length === 0 ? (
          <EmptyState title="No appointments yet." />
        ) : (
          <SurfaceCard className="overflow-hidden p-0">
            <table className="w-full text-[13.5px]" aria-label="Appointments">
              <thead>
                <tr className="border-b border-[hsl(var(--cx-line))] text-left text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]">
                  <th className="px-[12px] py-[10px] font-semibold">Contact</th>
                  <th className="px-[12px] py-[10px] font-semibold">When</th>
                  <th className="px-[12px] py-[10px] font-semibold">Notes</th>
                  <th className="px-[12px] py-[10px] font-semibold">Status</th>
                  <th className="px-[12px] py-[10px] font-semibold">Booked by</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[hsl(var(--cx-line))]">
                {(appointments ?? []).map((appt) => (
                  <AppointmentRow key={appt.id} appt={appt} />
                ))}
              </tbody>
            </table>
          </SurfaceCard>
        )}
      </div>
    </div>
  );
}
