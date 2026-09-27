import { Check, X } from "lucide-react";

export const PASSWORD_MIN_LENGTH = 12;

export interface PasswordCheck {
  id: "length" | "variety" | "email";
  label: string;
  ok: boolean;
}

export function passwordChecks(password: string, email: string): PasswordCheck[] {
  const lowered = password.toLowerCase();
  const local = email.split("@")[0].toLowerCase();
  return [
    {
      id: "length",
      label: `At least ${PASSWORD_MIN_LENGTH} characters`,
      ok: password.length >= PASSWORD_MIN_LENGTH,
    },
    {
      id: "variety",
      label: "Not too repetitive",
      ok: new Set(lowered).size >= 4,
    },
    {
      id: "email",
      label: "Does not contain your email name",
      ok: local.length < 4 || !lowered.includes(local),
    },
  ];
}

export function passwordApproved(password: string, email: string): boolean {
  return passwordChecks(password, email).every((check) => check.ok);
}

function CheckRow({ ok, label }: { ok: boolean; label: string }): JSX.Element {
  return (
    <li className="flex items-center gap-2" data-ok={ok ? "true" : "false"}>
      <span className="sr-only">{ok ? "Met: " : "Not met: "}</span>
      <span
        aria-hidden="true"
        className={
          "inline-flex h-4 w-4 items-center justify-center rounded-full text-white " +
          (ok ? "bg-emerald-500" : "bg-red-500")
        }
      >
        {ok ? (
          <Check className="h-3 w-3" strokeWidth={3} />
        ) : (
          <X className="h-3 w-3" strokeWidth={3} />
        )}
      </span>
      <span className={ok ? "text-xs text-foreground" : "text-xs text-muted-foreground"}>
        {label}
      </span>
    </li>
  );
}

export function PasswordChecklist({
  password,
  email,
}: {
  password: string;
  email: string;
}): JSX.Element | null {
  if (password === "") {
    return null;
  }

  const checks = passwordChecks(password, email);
  const passed = checks.filter((check) => check.ok).length;
  const allOk = passed === checks.length;
  const fillClass =
    passed === 3 ? "bg-emerald-500" : passed === 2 ? "bg-amber-500" : "bg-red-500";

  return (
    <div className="mt-2 space-y-2" aria-live="polite">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={"h-full transition-all " + fillClass}
          style={{ width: `${(passed / checks.length) * 100}%` }}
        />
      </div>
      <ul className="space-y-1">
        {checks.map((check) => (
          <CheckRow key={check.id} ok={check.ok} label={check.label} />
        ))}
        {allOk ? (
          <li className="flex items-center gap-2" data-ok="true">
            <span className="sr-only">Met: </span>
            <span
              aria-hidden="true"
              className="inline-flex h-4 w-4 items-center justify-center rounded-full bg-emerald-500 text-white"
            >
              <Check className="h-3 w-3" strokeWidth={3} />
            </span>
            <span className="text-xs font-bold text-emerald-600 dark:text-emerald-400">
              Password approved
            </span>
          </li>
        ) : null}
      </ul>
    </div>
  );
}
