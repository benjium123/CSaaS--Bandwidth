/**
 * The "needs you" filter chips.
 *
 * Every chip is a toggle: picking the chip that is already active clears the filter, which is why
 * the handler receives `null` rather than the key a second time. A chip with nothing behind it is
 * not rendered at all, so the whole group disappears when nothing needs a member's attention.
 */
import * as React from "react";
import { AlarmClock, MailOpen, MessageSquare, PhoneMissed, UserCheck, Voicemail } from "lucide-react";
import { cn } from "@/lib/utils";

export type NeedsYouKey = "missed" | "unresponded" | "voicemail" | "assigned" | "unread" | "overdue";

type ChipIcon = React.ComponentType<{
  className?: string;
  "aria-hidden"?: boolean | "true" | "false";
}>;

// Most urgent first: a missed call costs a deal fastest.
const CHIPS: { key: NeedsYouKey; label: string; Icon: ChipIcon }[] = [
  { key: "missed", label: "Missed", Icon: PhoneMissed },
  { key: "unresponded", label: "Waiting", Icon: MessageSquare },
  { key: "voicemail", label: "Voicemails", Icon: Voicemail },
  { key: "assigned", label: "Assigned", Icon: UserCheck },
  { key: "unread", label: "Unread", Icon: MailOpen },
  { key: "overdue", label: "Overdue", Icon: AlarmClock },
];

const URGENT: NeedsYouKey[] = ["missed", "overdue"];

export function NeedsYouChips(props: {
  counts: Partial<Record<NeedsYouKey, { n: number; more: boolean }>>;
  active: NeedsYouKey | null;
  onPick(key: NeedsYouKey | null): void;
}): JSX.Element | null {
  const { counts, active, onPick } = props;

  const chips = CHIPS.flatMap((chip) => {
    const count = counts[chip.key];
    return count && count.n > 0 ? [{ ...chip, count }] : [];
  });

  if (chips.length === 0) return null;

  return (
    <div role="group" aria-label="Needs you" className="flex flex-wrap gap-1.5">
      {chips.map(({ key, label, Icon, count }) => {
        const pressed = active === key;
        const text = `${count.n}${count.more ? "+" : ""}`;

        return (
          <button
            key={key}
            type="button"
            aria-pressed={pressed}
            aria-label={`${label}: ${text}`}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-full border border-border px-2 py-0.5 text-xs text-foreground",
              pressed && "border-primary bg-primary/10",
            )}
            onClick={() => onPick(pressed ? null : key)}
          >
            <Icon aria-hidden="true" className="h-3.5 w-3.5" />
            <span className={cn("font-semibold", URGENT.includes(key) && "text-destructive")}>
              {text}
            </span>
            <span>{label}</span>
          </button>
        );
      })}
    </div>
  );
}
