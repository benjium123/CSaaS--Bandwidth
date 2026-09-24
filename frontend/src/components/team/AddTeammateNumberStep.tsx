/**
 * Step 1 of the Add teammate drawer: which number will the new teammate hold?
 *
 * One person = one number, $15/month each, so reusing a number the workspace ALREADY
 * pays for is the default path and buying is the fallback. `mode` is local, but the
 * chosen number is lifted to the drawer through `onChange`, so a pick from the other
 * mode can never survive a switch.
 *
 * The drawer unmounts this component between steps, so the chosen number (`value`) can
 * outlive the state that produced it: `mode` is initialised FROM `value`, and an armed
 * buy selection is always drawn as a checked row even when no search returned it, so a
 * pending charge is never invisible on this screen.
 *
 * Every query is gated on `open`: a closed drawer issues zero requests.
 */
import * as React from "react";

import {
  formatMonthlyCost,
  useAvailableNumbers,
  type AvailableNumberFilters,
  type SearchOut,
} from "@/api/numbers";
import { unheldNumbers, useAllAssignments } from "@/api/orgMembers";
import { useAuth } from "@/auth/AuthContext";
import {
  ConsoleCard,
  ConsoleEmpty,
  FilterPill,
  SectionLabel,
} from "@/components/ui/consoleChrome";
import { Button, Input, Select, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

export type NumberChoice =
  | { kind: "none" }
  | { kind: "existing"; inboxId: string; e164: string }
  | { kind: "buy"; result: SearchOut };

type Mode = "existing" | "buy";

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

const RADIO_CLASS = "h-4 w-4 flex-none accent-[hsl(var(--cx-accent))]";
const NOTE_CLASS = "text-[12px] text-[hsl(var(--cx-muted))]";
const ALERT_CLASS = "text-[13px] text-[hsl(var(--cx-danger))]";

export function AddTeammateNumberStep({
  open,
  canOrder,
  canGrant,
  paidCheckout = false,
  value,
  onChange,
}: {
  /** The drawer's open state. EVERY query here is `enabled: open`. */
  open: boolean;
  /** Per-number billing: a new number is bought through secure checkout, never here. */
  paidCheckout?: boolean;
  /** numbers:manage - may buy. */
  canOrder: boolean;
  /** inboxes:admin - may see who holds what, and may assign. */
  canGrant: boolean;
  value: NumberChoice;
  onChange: (next: NumberChoice) => void;
}) {
  const { api } = useAuth();
  // Lazy: `value` is read on mount only. Recomputing it per render would let a later
  // `onChange` fight the pills.
  const [mode, setMode] = React.useState<Mode>(() =>
    value.kind === "buy" ? "buy" : "existing",
  );
  // Buying needs BOTH: the right to order and the right to assign.
  const canBuy = canOrder && canGrant;

  const assignmentsQuery = useAllAssignments(
    api,
    open && mode === "existing" && canGrant,
  );
  const free = React.useMemo(
    () => unheldNumbers(assignmentsQuery.data ?? []),
    [assignmentsQuery.data],
  );

  const [areaCode, setAreaCode] = React.useState("");
  const [contains, setContains] = React.useState("");
  const [numberType, setNumberType] = React.useState("local");
  const [searchFilters, setSearchFilters] =
    React.useState<AvailableNumberFilters | null>(null);

  const availableQuery = useAvailableNumbers(
    api,
    searchFilters ?? {},
    open && mode === "buy" && searchFilters !== null,
  );
  const results = availableQuery.data ?? [];

  // Reopening lands on the default, no-charge path - never on the previous teammate's
  // search.
  React.useEffect(() => {
    if (!open) {
      setMode("existing");
      setSearchFilters(null);
    }
  }, [open]);

  const pickMode = (next: Mode) => {
    setMode(next);
    onChange({ kind: "none" });
  };

  const submitSearch = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onChange({ kind: "none" });
    // No carrier: this form has no provider picker on purpose.
    setSearchFilters({
      area_code: areaCode || undefined,
      contains: contains || undefined,
      number_type: numberType,
    });
  };

  /** One buy row. Shared by real results and the armed selection so the markup, classes
   *  and aria-labels cannot drift apart. */
  const buyRow = (
    result: SearchOut,
    subtitle: string,
    checked: boolean,
    handleSelect: () => void,
  ) => (
    <ConsoleCard key={result.e164}>
      <label className="flex items-center gap-3">
        <input
          type="radio"
          name="add-teammate-number"
          className={RADIO_CLASS}
          aria-label={"Buy " + formatPhone(result.e164)}
          checked={checked}
          onChange={handleSelect}
        />
        <div className="min-w-0 flex-1">
          <div className="text-[14px] font-medium text-[hsl(var(--cx-text))]">
            {formatPhone(result.e164)}
          </div>
          <div className={NOTE_CLASS}>{subtitle}</div>
        </div>
        <div className="whitespace-nowrap text-[13px] text-[hsl(var(--cx-text))]">
          {formatMonthlyCost(result) + "/mo"}
        </div>
      </label>
    </ConsoleCard>
  );

  const existingBody = !canGrant ? (
    <ConsoleEmpty>
      You don't have permission to see or assign numbers. You can still create the
      account now, and someone with number access can give them a line from Team &gt;
      Manage numbers.
    </ConsoleEmpty>
  ) : assignmentsQuery.isLoading ? (
    <Spinner label="Loading numbers" />
  ) : assignmentsQuery.error ? (
    <p role="alert" className={ALERT_CLASS}>
      {errorMessage(assignmentsQuery.error)}
    </p>
  ) : free.length === 0 ? (
    paidCheckout ? (
      <ConsoleEmpty>
        Each teammate gets their own number. Every number here is already in use, so{" "}
        <a href="/choose-numbers?next=%2Fteam%3Fadd%3D1" className="font-medium underline">
          buy a number for them ($15/month)
        </a>{" "}
        and you will come straight back here to create their login.
      </ConsoleEmpty>
    ) : (
      <ConsoleEmpty>
        Every number in this workspace is already held by someone. Buy a new one for this
        teammate, or free up a number from Team &gt; Manage numbers.
      </ConsoleEmpty>
    )
  ) : (
    <>
      <div className="flex flex-col gap-2">
        {free.map((n) => (
          <ConsoleCard key={n.inbox_id}>
            <label className="flex items-center gap-3">
              <input
                type="radio"
                name="add-teammate-number"
                className={RADIO_CLASS}
                aria-label={"Use " + formatPhone(n.e164)}
                checked={value.kind === "existing" && value.inboxId === n.inbox_id}
                onChange={() =>
                  onChange({ kind: "existing", inboxId: n.inbox_id, e164: n.e164 })
                }
              />
              <div>
                <div className="text-[14px] font-medium text-[hsl(var(--cx-text))]">
                  {formatPhone(n.e164)}
                </div>
                <div className={NOTE_CLASS}>{n.inbox_name}</div>
              </div>
            </label>
          </ConsoleCard>
        ))}
      </div>
      <p className={NOTE_CLASS}>
        Reusing a number costs nothing extra - this workspace already pays for it.
      </p>
    </>
  );

  // The selection survives the step unmount, so show it even if no current search
  // returned it - otherwise a still-armed purchase would be invisible here.
  const armedBuyRow =
    value.kind === "buy" && !results.some((r) => r.e164 === value.result.e164)
      ? buyRow(value.result, "Selected - search again to change it.", true, () => {})
      : null;

  const buyBody = (
    <>
      <form className="flex flex-wrap items-end gap-3" onSubmit={submitSearch}>
        <div>
          <label
            htmlFor="teammate-area-code"
            className="block text-xs text-[hsl(var(--cx-muted))]"
          >
            Area code
          </label>
          <Input
            id="teammate-area-code"
            aria-label="Area code"
            placeholder="214"
            className="w-24"
            value={areaCode}
            onChange={(event) => setAreaCode(event.target.value)}
          />
        </div>
        <div>
          <label
            htmlFor="teammate-contains"
            className="block text-xs text-[hsl(var(--cx-muted))]"
          >
            Contains
          </label>
          <Input
            id="teammate-contains"
            aria-label="Contains"
            className="w-28"
            value={contains}
            onChange={(event) => setContains(event.target.value)}
          />
        </div>
        <div>
          <label
            htmlFor="teammate-number-type"
            className="block text-xs text-[hsl(var(--cx-muted))]"
          >
            Type
          </label>
          <Select
            id="teammate-number-type"
            aria-label="Number type"
            value={numberType}
            onChange={(event) => setNumberType(event.target.value)}
          >
            <option value="local">Local</option>
            <option value="tollfree">Toll-free</option>
          </Select>
        </div>
        <Button
          type="submit"
          className="rounded-full px-5"
          disabled={availableQuery.isFetching}
        >
          Search
        </Button>
      </form>

      {armedBuyRow}

      {availableQuery.isFetching ? (
        <Spinner label="Searching" />
      ) : availableQuery.isError ? (
        <p role="alert" className={ALERT_CLASS}>
          {errorMessage(availableQuery.error)}
        </p>
      ) : results.length > 0 ? (
        <div className="flex flex-col gap-2">
          {results.map((r) =>
            buyRow(
              r,
              [r.locality, r.region].filter(Boolean).join(", "),
              value.kind === "buy" && value.result.e164 === r.e164,
              () => onChange({ kind: "buy", result: r }),
            ),
          )}
        </div>
      ) : searchFilters !== null ? (
        <ConsoleEmpty>
          No numbers matched. Try a different area code or phrase.
        </ConsoleEmpty>
      ) : null}

      <p className={NOTE_CLASS}>
        Buying adds a new monthly charge to this workspace. You'll see the exact amount
        before anything is ordered.
      </p>
    </>
  );

  return (
    <div className="flex flex-col gap-[11px]">
      <SectionLabel>Their number</SectionLabel>

      <div className="flex flex-wrap gap-2">
        <FilterPill
          active={mode === "existing"}
          aria-pressed={mode === "existing"}
          onClick={() => pickMode("existing")}
        >
          Use a number we already have
        </FilterPill>
        {canBuy ? (
          <FilterPill
            active={mode === "buy"}
            aria-pressed={mode === "buy"}
            onClick={() => pickMode("buy")}
          >
            Buy a new number
          </FilterPill>
        ) : null}
      </div>

      {canBuy ? null : (
        <p className={NOTE_CLASS}>
          {!canOrder
            ? "You can't buy numbers, so only numbers this workspace already owns are shown."
            : "Buying a number here also needs permission to assign it, which you don't have. Buy it in Settings > Phone numbers instead."}
        </p>
      )}

      {mode === "existing" ? existingBody : buyBody}
    </div>
  );
}
