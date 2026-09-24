/**
 * The whole "Add teammate" journey in one drawer, opened from Settings > Team.
 *
 * Buying a number, creating the user and assigning the number are three journeys in three
 * places today; this is all three, in order, in one place. One user = one phone number.
 *
 * Ordering a number is a carrier call and cannot join a database transaction, so the user
 * and the number cannot be created atomically. The user is created FIRST. That puts the
 * likely failures - duplicate email, a password the policy rejects, a role the actor may
 * not grant, a step-up challenge - in front of the money. The only failure left after the
 * charge is the grant, which is free, is visible on both the Team and the Numbers page,
 * and can be retried in place. Ordering first would mean a rejected email address leaves
 * the workspace paying every month for a number nobody holds, with nothing on screen
 * saying so.
 */
import * as React from "react";

import { useGate } from "@/api/capabilities";
import type { MemberOut } from "@/api/hooks";
import {
  putNumberAssignments,
  type NumberAssignment,
} from "@/api/numberAssignments";
import {
  formatMonthlyCost,
  formatSetupCost,
  useOrderNumber,
} from "@/api/numbers";
import { useCreateMember } from "@/api/orgMembers";
import { useAuth } from "@/auth/AuthContext";
import {
  ConsoleCard,
  ConsoleEmpty,
  SectionLabel,
} from "@/components/ui/consoleChrome";
import {
  Button,
  Drawer,
  Input,
  MutationStatus,
  Select,
  Spinner,
} from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

import {
  AddTeammateNumberStep,
  type NumberChoice,
} from "./AddTeammateNumberStep";

type Step = "form" | "confirm" | "done";

type Outcome =
  | { kind: "ok"; name: string; e164: string | null; priceText: string | null }
  | { kind: "member-failed"; message: string }
  | { kind: "order-failed"; name: string; message: string }
  | {
      kind: "grant-failed";
      name: string;
      e164: string;
      priceText: string;
      userId: string;
      message: string;
    };

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "Something went wrong.";
}

/** Never a bare red toast: every ending says exactly what happened to the money. */
function outcomeText(outcome: Outcome): string {
  switch (outcome.kind) {
    case "ok":
      if (outcome.e164 != null && outcome.priceText != null) {
        return `${outcome.name} can sign in now, and ${formatPhone(outcome.e164)} is theirs. The workspace is being charged ${outcome.priceText} a month for it.`;
      }
      if (outcome.e164 != null) {
        return `${outcome.name} can sign in now, and ${formatPhone(outcome.e164)} is theirs.`;
      }
      return `${outcome.name} can sign in now. They have no number yet - give them one from Team > Manage numbers.`;
    case "member-failed":
      return `No teammate was created and nothing was bought. You have not been charged. ${outcome.message}`;
    case "order-failed":
      return `${outcome.name}'s account was created and they can sign in with the password you set. The number was NOT bought and you have not been charged. Buy a number in Settings > Phone numbers, then give it to ${outcome.name} from Team > Manage numbers. ${outcome.message}`;
    case "grant-failed":
      return `${outcome.name}'s account was created and ${formatPhone(outcome.e164)} was bought - this workspace is now being charged ${outcome.priceText} a month for it. Only the last step failed: the number is not assigned to ${outcome.name} yet. Retry below, or assign it from Team > Manage numbers. Nothing will be bought again. ${outcome.message}`;
  }
}

export function AddTeammateDrawer({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { api, me, orgId } = useAuth();
  const gate = useGate();
  const canInvite = gate.can("members:invite");
  // Per-number billing: numbers are bought through Stripe checkout, one per teammate.
  const paidCheckout =
    me?.memberships?.find((m) => m.org_id === orgId)?.number_subscription_required === true;
  const canOrder = gate.can("numbers:manage") && !paidCheckout;
  const canGrant = gate.can("inboxes:admin");

  const createMember = useCreateMember(api);
  const orderNumber = useOrderNumber(api);

  const [choice, setChoice] = React.useState<NumberChoice>({ kind: "none" });
  const [fullName, setFullName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [roleName, setRoleName] = React.useState<"admin" | "agent">("agent");
  const [step, setStep] = React.useState<Step>("form");
  const [outcome, setOutcome] = React.useState<Outcome | null>(null);
  const [running, setRunning] = React.useState(false);

  // Reopening must never show the previous teammate's details or result.
  React.useEffect(() => {
    if (!open) {
      setChoice({ kind: "none" });
      setFullName("");
      setEmail("");
      setPassword("");
      setRoleName("agent");
      setStep("form");
      setOutcome(null);
      setRunning(false);
    }
  }, [open]);

  const chosenE164 =
    choice.kind === "existing"
      ? choice.e164
      : choice.kind === "buy"
        ? choice.result.e164
        : null;
  const buying = choice.kind === "buy";
  const priceText =
    choice.kind === "buy" && choice.result.monthly_cost_cents != null
      ? formatMonthlyCost(choice.result)
      : "$15.00";

  const canSubmit =
    fullName.trim() !== "" && email.trim() !== "" && password !== "";

  async function grantOrRecord(userId: string, e164: string): Promise<void> {
    try {
      // POST /numbers/order returns no inbox_id, so re-read this user's assignments (which
      // list every inbox in the org) and match the number we just bought by e164.
      const assignments = await api.request<NumberAssignment[]>(
        "/api/v1/inboxes/assignments?user_id=" + encodeURIComponent(userId),
      );
      const match = assignments.find((a) => a.e164 === e164);
      if (!match) throw new Error("The number is still being provisioned.");
      await putNumberAssignments(api, userId, [
        { inbox_id: match.inbox_id, role: "member" },
      ]);
      setOutcome({ kind: "ok", name: fullName, e164, priceText });
    } catch (err) {
      setOutcome({
        kind: "grant-failed",
        name: fullName,
        e164,
        priceText,
        userId,
        message: errorMessage(err),
      });
    }
    setStep("done");
  }

  // The retry re-runs ONLY the grant: never the member create, never the order.
  async function retryGrant(userId: string, e164: string): Promise<void> {
    setRunning(true);
    try {
      await grantOrRecord(userId, e164);
    } finally {
      setRunning(false);
    }
  }

  async function runFlow(): Promise<void> {
    setRunning(true);
    try {
      // Step 1 - always, and the ONLY call when nothing is being bought. The backend
      // creates the user, the membership and the inbox grants in one transaction.
      let member: MemberOut;
      try {
        member = await createMember.mutateAsync({
          email,
          full_name: fullName,
          password,
          role_name: roleName,
          inbox_ids: choice.kind === "existing" ? [choice.inboxId] : [],
        });
      } catch (err) {
        setOutcome({ kind: "member-failed", message: errorMessage(err) });
        setStep("done");
        return;
      }

      if (choice.kind !== "buy") {
        setOutcome({
          kind: "ok",
          name: fullName,
          e164: chosenE164,
          priceText: null,
        });
        setStep("done");
        return;
      }

      // Step 2 - the money. Omit the cost fields entirely rather than sending null when
      // the provider did not quote them (same rule as NumbersPage's order()).
      let orderedE164: string;
      try {
        const ordered = await orderNumber.mutateAsync({
          e164: choice.result.e164,
          ...(typeof choice.result.monthly_cost_cents === "number"
            ? { monthly_cost_cents: choice.result.monthly_cost_cents }
            : {}),
          ...(typeof choice.result.setup_cost_cents === "number"
            ? { setup_cost_cents: choice.result.setup_cost_cents }
            : {}),
        });
        orderedE164 = ordered.e164;
      } catch (err) {
        setOutcome({
          kind: "order-failed",
          name: fullName,
          message: errorMessage(err),
        });
        setStep("done");
        return;
      }

      // Step 3 - the grant. Free, and the only thing that can fail after the charge.
      await grantOrRecord(member.user_id, orderedE164);
    } finally {
      setRunning(false);
    }
  }

  return (
    <Drawer open={open} onClose={onClose} title="Add teammate" width="w-[520px]">
      {gate.isLoading ? (
        <Spinner label="Checking permissions" />
      ) : !canInvite ? (
        <ConsoleEmpty>You don't have permission to add teammates.</ConsoleEmpty>
      ) : (
        <div className="flex flex-col gap-[14px]">
          {step === "form" ? (
            <>
              {/* Siblings, not nested: the number step has its own <form>. */}
              <AddTeammateNumberStep
                open={open}
                canOrder={canOrder}
                canGrant={canGrant}
                paidCheckout={paidCheckout}
                value={choice}
                onChange={setChoice}
              />

              <SectionLabel>Their details</SectionLabel>
              <form
                className="flex flex-col gap-[11px]"
                onSubmit={(event) => {
                  event.preventDefault();
                  setStep("confirm");
                }}
              >
                <div className="flex flex-col gap-1.5">
                  <label
                    className="text-xs text-[hsl(var(--cx-muted))]"
                    htmlFor="teammate-name"
                  >
                    Full name
                  </label>
                  <Input
                    id="teammate-name"
                    value={fullName}
                    required
                    onChange={(event) => setFullName(event.target.value)}
                  />
                </div>

                <div className="flex flex-col gap-1.5">
                  <label
                    className="text-xs text-[hsl(var(--cx-muted))]"
                    htmlFor="teammate-email"
                  >
                    Email
                  </label>
                  <Input
                    id="teammate-email"
                    type="email"
                    value={email}
                    required
                    onChange={(event) => setEmail(event.target.value)}
                  />
                </div>

                <div className="flex flex-col gap-1.5">
                  <label
                    className="text-xs text-[hsl(var(--cx-muted))]"
                    htmlFor="teammate-password"
                  >
                    Password
                  </label>
                  <Input
                    id="teammate-password"
                    type="password"
                    value={password}
                    required
                    onChange={(event) => setPassword(event.target.value)}
                  />
                </div>

                {/* No owner option on purpose: ownership is transferred, never minted here. */}
                <div className="flex flex-col gap-1.5">
                  <label
                    className="text-xs text-[hsl(var(--cx-muted))]"
                    htmlFor="teammate-role"
                  >
                    Role
                  </label>
                  <Select
                    id="teammate-role"
                    value={roleName}
                    onChange={(event) =>
                      setRoleName(event.target.value === "admin" ? "admin" : "agent")
                    }
                  >
                    <option value="agent">Agent</option>
                    <option value="admin">Admin</option>
                  </Select>
                </div>

                <Button
                  type="submit"
                  className="rounded-full px-5"
                  disabled={!canSubmit}
                >
                  Review
                </Button>
              </form>
            </>
          ) : null}

          {step === "confirm" ? (
            <>
              <SectionLabel>Confirm</SectionLabel>
              <ConsoleCard>
                <div className="flex justify-between gap-3 text-[13px]">
                  <span className="text-[hsl(var(--cx-muted))]">Name</span>
                  <span className="text-[hsl(var(--cx-text))]">{fullName}</span>
                </div>
                <div className="flex justify-between gap-3 text-[13px]">
                  <span className="text-[hsl(var(--cx-muted))]">Email</span>
                  <span className="text-[hsl(var(--cx-text))]">{email}</span>
                </div>
                <div className="flex justify-between gap-3 text-[13px]">
                  <span className="text-[hsl(var(--cx-muted))]">Role</span>
                  <span className="text-[hsl(var(--cx-text))]">
                    {roleName === "admin" ? "Admin" : "Agent"}
                  </span>
                </div>
                <div className="flex justify-between gap-3 text-[13px]">
                  <span className="text-[hsl(var(--cx-muted))]">Number</span>
                  <span className="text-[hsl(var(--cx-text))]">
                    {chosenE164 ? formatPhone(chosenE164) : "No number yet"}
                  </span>
                </div>
              </ConsoleCard>

              {choice.kind === "buy" ? (
                <ConsoleCard role="note">
                  <p className="text-[13px] text-[hsl(var(--cx-text))]">
                    {`Ordering ${formatPhone(choice.result.e164)} will charge this workspace ${priceText} a month, starting today. This is a live carrier order - it is billed until someone releases the number in Settings > Phone numbers.`}
                  </p>
                  {choice.result.setup_cost_cents != null &&
                  choice.result.setup_cost_cents > 0 ? (
                    <p className="text-[13px] text-[hsl(var(--cx-text))]">
                      {`There is also a one-time setup cost of ${formatSetupCost(choice.result.setup_cost_cents)}.`}
                    </p>
                  ) : null}
                </ConsoleCard>
              ) : (
                <p
                  role="note"
                  className="text-[12.5px] text-[hsl(var(--cx-muted))]"
                >
                  {choice.kind === "existing"
                    ? "No new charge - this workspace already pays for this number."
                    : "No number will be bought and nothing will be charged."}
                </p>
              )}

              <div className="flex flex-wrap items-center gap-3">
                <Button
                  type="button"
                  variant="outline"
                  className="rounded-full px-5"
                  disabled={running}
                  onClick={() => setStep("form")}
                >
                  Back
                </Button>
                <Button
                  type="button"
                  className="rounded-full px-5"
                  disabled={running}
                  onClick={() => void runFlow()}
                >
                  {buying
                    ? "Create teammate and buy the number"
                    : "Create teammate"}
                </Button>
                <MutationStatus pending={running} pendingLabel="Working…" />
              </div>
            </>
          ) : null}

          {step === "done" && outcome !== null ? (
            <ConsoleCard
              className="flex flex-col gap-[11px]"
              role={outcome.kind === "ok" ? "status" : "alert"}
            >
              <p className="text-[13px] text-[hsl(var(--cx-text))]">
                {outcomeText(outcome)}
              </p>
              <div className="flex flex-wrap items-center gap-3">
                {outcome.kind === "grant-failed" ? (
                  <Button
                    type="button"
                    className="rounded-full px-5"
                    disabled={running}
                    onClick={() => void retryGrant(outcome.userId, outcome.e164)}
                  >
                    Retry assigning the number
                  </Button>
                ) : null}
                <Button
                  type="button"
                  variant="outline"
                  className="rounded-full px-5"
                  onClick={onClose}
                >
                  {outcome.kind === "ok" ? "Done" : "Close"}
                </Button>
              </div>
            </ConsoleCard>
          ) : null}
        </div>
      )}
    </Drawer>
  );
}
