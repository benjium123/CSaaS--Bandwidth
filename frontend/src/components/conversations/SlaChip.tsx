import * as React from "react";
import { slaState, type Sla } from "@/api/inboxPro";
import { cn } from "@/lib/utils";

export function SlaChip({
  sla,
  className,
}: {
  sla: Sla | null | undefined;
  className?: string;
}) {
  const [now, setNow] = React.useState(() => new Date());
  const state = slaState(sla, now);

  React.useEffect(() => {
    if (state.kind === "none") return;
    const id = window.setInterval(() => setNow(new Date()), 60000);
    return () => window.clearInterval(id);
  }, [state.kind]);

  if (state.kind === "none") return null;

  const label = state.label;
  const overdue = state.kind === "overdue";

  return (
    <span
      aria-label={`Reply time: ${label}`}
      title={
        overdue
          ? "This conversation is past its reply time"
          : "When the first reply is due"
      }
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium",
        overdue ? "text-destructive" : "bg-muted text-muted-foreground",
        className,
      )}
    >
      {label}
    </span>
  );
}
