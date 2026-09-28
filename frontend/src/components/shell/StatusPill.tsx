/**
 * The status pill: one control that says where calls go, and the menu that changes it.
 *
 * It renders NOTHING it cannot stand behind - no permission, or prefs not loaded yet - so a
 * member who may not place calls never sees a switch they cannot use, and no one sees a
 * half-populated menu while the GET is in flight.
 *
 * The menu closes on Escape and on an outside press only. Choosing an item leaves it open,
 * which is what lets a failed save stay visible in the role="alert" below the items rather
 * than being dismissed along with the menu.
 */
import * as React from "react";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { dndTimeLeft, nextEightAm, useCallPrefs } from "@/api/callPrefs";

const MINUTE_MS = 60_000;

export function StatusPill() {
  const { me, orgId } = useAuth();
  const { prefs, save, error } = useCallPrefs();
  const [open, setOpen] = React.useState(false);
  const triggerRef = React.useRef<HTMLButtonElement | null>(null);
  const menuRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    if (!open) return undefined;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };

    const onMouseDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (menuRef.current?.contains(target)) return;
      if (triggerRef.current?.contains(target)) return;
      setOpen(false);
    };

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onMouseDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onMouseDown);
    };
  }, [open]);

  const allowed = hasPermission(me, orgId, "calls:place");

  if (!allowed || !prefs) return null;

  const dnd = prefs.dnd;
  const dndUntil = prefs.dnd_until;
  const timeLeft = dnd ? dndTimeLeft(dndUntil) : null;
  const teammates = prefs.teammates ?? [];

  const chooseDnd = (until: Date | null) => {
    void save({ dnd: true, dnd_until: until ? until.toISOString() : null });
  };

  return (
    <div className="relative">
      <Button
        ref={triggerRef}
        type="button"
        variant="ghost"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Status: ${dnd ? "Do not disturb" : "Available"}`}
        className={cn("gap-2", dnd && "text-destructive")}
        onClick={() => setOpen((value) => !value)}
      >
        <span
          aria-hidden="true"
          className={cn("h-2 w-2 rounded-full", dnd ? "bg-destructive" : "bg-emerald-500")}
        />
        <span>{dnd ? "Do not disturb" : "Available"}</span>
        {timeLeft ? <span className="text-xs text-muted-foreground">{timeLeft}</span> : null}
      </Button>

      {open ? (
        <div
          ref={menuRef}
          role="menu"
          aria-label="Status"
          className="absolute right-0 z-50 mt-1 w-64 rounded-md border border-border bg-background p-1 shadow-lg"
        >
          {error ? (
            <div role="alert" className="px-2 py-1 text-sm text-destructive">
              {error}
            </div>
          ) : null}

          <Button
            type="button"
            variant="ghost"
            role="menuitemradio"
            aria-checked={!dnd}
            className="w-full justify-start font-normal"
            onClick={() => void save({ dnd: false })}
          >
            Available
          </Button>

          <Button
            type="button"
            variant="ghost"
            role="menuitemradio"
            aria-checked={false}
            className="w-full justify-start font-normal"
            onClick={() => chooseDnd(new Date(Date.now() + 30 * MINUTE_MS))}
          >
            Do not disturb for 30 minutes
          </Button>

          <Button
            type="button"
            variant="ghost"
            role="menuitemradio"
            aria-checked={false}
            className="w-full justify-start font-normal"
            onClick={() => chooseDnd(new Date(Date.now() + 60 * MINUTE_MS))}
          >
            Do not disturb for 1 hour
          </Button>

          <Button
            type="button"
            variant="ghost"
            role="menuitemradio"
            aria-checked={false}
            className="w-full justify-start font-normal"
            onClick={() => chooseDnd(nextEightAm())}
          >
            Do not disturb until 8:00 AM
          </Button>

          <Button
            type="button"
            variant="ghost"
            role="menuitemradio"
            aria-checked={dnd && dndUntil == null}
            className="w-full justify-start font-normal"
            onClick={() => chooseDnd(null)}
          >
            Do not disturb until I turn it off
          </Button>

          {dnd ? (
            <div className="px-2 py-1">
              <span aria-hidden="true" className="mb-1 block text-xs text-muted-foreground">
                Send my calls to
              </span>
              <select
                aria-label="Send my calls to"
                className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm"
                value={prefs.forward_to ?? ""}
                onChange={(event) => {
                  const value = event.target.value;
                  void save({
                    dnd: true,
                    dnd_until: dndUntil,
                    forward_to: value || null,
                  });
                }}
              >
                <option value="">Voicemail</option>
                {teammates.map((teammate) => (
                  <option key={teammate.user_id} value={teammate.user_id}>
                    {teammate.name}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
