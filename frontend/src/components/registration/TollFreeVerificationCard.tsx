/**
 * Toll-free verification (TFV) card for the console.
 *
 * TFV is a SEPARATE path from 10DLC: 10DLC registers a company plus campaigns, whereas TFV
 * verifies ONE toll-free number and the messaging it carries. Nothing here reads or writes
 * the 10DLC domain, and nothing here is shared with `@/api/registration`.
 *
 * COPY RULE - do not soften this: pressing Submit does NOT contact a carrier. The backend's
 * submit step only advances an internal state machine in THIS workspace; an operator
 * completes the filing with the carrier, and the status here changes later when they report
 * back. Nothing on this card may say or imply the verification was sent/transmitted/filed
 * anywhere, and `SUBMIT_DISCLOSURE` below is the exact copy that says so.
 *
 * The E.164 is not part of `TfvOut`, so the list joins to the numbers domain on `number_id`
 * and falls back to "Number unavailable" (never a raw uuid) when there is no match.
 */
import * as React from "react";

import { useGate } from "@/api/capabilities";
import { useNumbers, type NumberOut } from "@/api/numbers";
import { getErrorMessage } from "@/api/spend";
import {
  TFV_USE_CASES,
  isTerminalTfvStatus,
  isTollFree,
  tfvStatusTone,
  useCreateTollFreeVerification,
  useSubmitTollFreeVerification,
  useTollFreeVerificationList,
  type TfvCreateInput,
} from "@/api/tollfree";
import { useAuth } from "@/auth/AuthContext";
import {
  Button,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Select,
  Spinner,
  Textarea,
} from "@/components/ui/primitives";
import {
  ConsoleCard,
  ConsoleEmpty,
  PageHeader,
  SectionLabel,
  SurfaceCard,
} from "@/components/ui/consoleChrome";

/** Exported for the tests: the exact sentence that must sit next to a Submit button. */
export const SUBMIT_DISCLOSURE =
  "Submitting marks this verification ready in this workspace. Nothing is transmitted to a carrier from here - an operator files it with the carrier, and the status updates when they report back.";

const PAGE_DESCRIPTION =
  "Toll-free verification confirms one toll-free number's right to send messages. It is separate from 10DLC brand and campaign registration.";

const NUMBER_UNAVAILABLE = "Number unavailable";

const LABEL_CLASS = "block text-[12.5px] font-medium text-[hsl(var(--cx-text))]";
const HELP_CLASS = "mt-1 block text-[11.5px] text-[hsl(var(--cx-muted))]";

function numberLabel(byId: Map<string, NumberOut>, numberId: string): string {
  const match = byId.get(numberId);
  return match ? match.e164 : NUMBER_UNAVAILABLE;
}

export function TollFreeVerificationCard() {
  const { api } = useAuth();
  const gate = useGate();
  // `useGate().can()` returns false while capabilities load - that is the correct
  // fail-closed direction, so a slow lookup never flashes the form.
  const canRead = gate.can("compliance:read");
  const canManage = gate.can("compliance:manage");

  // Every hook is called unconditionally (React hook rules); only the render branches.
  const tfvQuery = useTollFreeVerificationList(api);
  const numbersQuery = useNumbers(api);
  const createMutation = useCreateTollFreeVerification(api);
  const submitMutation = useSubmitTollFreeVerification(api);

  const [numberId, setNumberId] = React.useState("");
  const [businessName, setBusinessName] = React.useState("");
  const [useCase, setUseCase] = React.useState(TFV_USE_CASES[0]?.value ?? "MIXED");
  const [useCaseSummary, setUseCaseSummary] = React.useState("");
  const [optInProcess, setOptInProcess] = React.useState("");
  const [screenshotUrl, setScreenshotUrl] = React.useState("");
  const [messageVolume, setMessageVolume] = React.useState("");
  const [contactEmail, setContactEmail] = React.useState("");

  const verifications = tfvQuery.data;
  const rows = verifications ?? [];
  const numbers = numbersQuery.data ?? [];
  const numbersById = new Map(numbers.map((number) => [number.id, number]));

  // A duplicate is a guaranteed 409 (unique on org + number), so anything already on file
  // is removed from the picker before the user can pick it.
  const claimedNumberIds = new Set(rows.map((row) => row.number_id));
  const eligibleNumbers = numbers.filter(
    (number) =>
      isTollFree(number) &&
      // A released/failed number is one the workspace no longer sends from, so offering it
      // would queue a verification that cannot hold. Only an active number is eligible.
      number.status === "active" &&
      !claimedNumberIds.has(number.id),
  );

  const trimmedVolume = messageVolume.trim();
  const parsedVolume = trimmedVolume === "" ? Number.NaN : Number(trimmedVolume);

  // The backend's submit step refuses anything missing a summary or an opt-in process, so
  // both are required here: otherwise the user creates a draft that can never be submitted.
  const canCreate =
    numberId.trim() !== "" &&
    businessName.trim() !== "" &&
    useCaseSummary.trim() !== "" &&
    optInProcess.trim() !== "";

  function resetForm() {
    setNumberId("");
    setBusinessName("");
    setUseCase(TFV_USE_CASES[0]?.value ?? "MIXED");
    setUseCaseSummary("");
    setOptInProcess("");
    setScreenshotUrl("");
    setMessageVolume("");
    setContactEmail("");
  }

  function handleCreate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canCreate) return;

    const payload: TfvCreateInput = {
      number_id: numberId,
      business_name: businessName.trim(),
      use_case: useCase,
      use_case_summary: useCaseSummary.trim(),
      opt_in_process: optInProcess.trim(),
    };

    // Optional fields are OMITTED ENTIRELY when blank - never sent as "" or undefined.
    const screenshot = screenshotUrl.trim();
    if (screenshot !== "") payload.opt_in_screenshot_url = screenshot;

    const email = contactEmail.trim();
    if (email !== "") payload.contact_email = email;

    if (Number.isFinite(parsedVolume)) payload.message_volume = parsedVolume;

    createMutation.mutate(payload, { onSuccess: () => resetForm() });
  }

  if (!canRead) {
    return (
      <SurfaceCard>
        <PageHeader
          headingLevel={2}
          title="Toll-free verification"
          description={PAGE_DESCRIPTION}
        />
        <p className="mt-3 text-[12.5px] text-[hsl(var(--cx-muted))]">
          You do not have permission to view toll-free verifications in this workspace.
        </p>
      </SurfaceCard>
    );
  }

  return (
    <SurfaceCard className="space-y-[11px]">
      <PageHeader
        headingLevel={2}
        title="Toll-free verification"
        description={PAGE_DESCRIPTION}
      />

      <div className="space-y-[11px]">
        <SectionLabel>Verifications</SectionLabel>

        {tfvQuery.isLoading ? (
          <Spinner label="Loading toll-free verifications" />
        ) : null}

        {tfvQuery.isError ? (
          <div role="alert" className="space-y-2">
            <p className="text-[12.5px] text-[hsl(var(--cx-danger))]">
              {getErrorMessage(tfvQuery.error)}
            </p>
            <Button
              type="button"
              variant="outline"
              onClick={() => void tfvQuery.refetch()}
            >
              Retry
            </Button>
          </div>
        ) : null}

        {verifications != null && verifications.length === 0 ? (
          <ConsoleEmpty>
            No toll-free verifications in this workspace yet.
          </ConsoleEmpty>
        ) : null}

        {verifications != null && verifications.length > 0 ? (
          <div className="space-y-[11px]">
            {verifications.map((verification) => (
              <ConsoleCard key={verification.id} className="space-y-2.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-[13px] font-medium text-[hsl(var(--cx-text))]">
                      {numberLabel(numbersById, verification.number_id)}
                    </div>
                    <div className="text-[12.5px] text-[hsl(var(--cx-muted))]">
                      {verification.business_name}
                    </div>
                  </div>
                  <Pill tone={tfvStatusTone(verification.status)}>
                    {verification.status}
                  </Pill>
                </div>

                {verification.status === "rejected" && verification.last_error ? (
                  <p className="text-[12px] text-[hsl(var(--cx-muted))]">
                    Reason: {verification.last_error}
                  </p>
                ) : null}

                {/* approved/rejected are terminal - a second submit 409s, so the button is
                    genuinely absent from the DOM on those rows, not disabled. */}
                {canManage && !isTerminalTfvStatus(verification.status) ? (
                  <div>
                    <Button
                      type="button"
                      variant="outline"
                      aria-label={`Submit verification for ${verification.business_name}`}
                      disabled={submitMutation.isPending}
                      onClick={() => submitMutation.mutate(verification.id)}
                    >
                      Submit
                    </Button>
                  </div>
                ) : null}
              </ConsoleCard>
            ))}
          </div>
        ) : null}

        {canManage && rows.some((row) => !isTerminalTfvStatus(row.status)) ? (
          <p className="text-[12px] text-[hsl(var(--cx-muted))]">
            {SUBMIT_DISCLOSURE}
          </p>
        ) : null}
      </div>

      {canManage ? (
        <div className="space-y-[11px]">
          <SectionLabel>New verification</SectionLabel>

          {/* Loading stays quiet: the empty state and the form both depend on numbers
              having actually arrived, so neither is shown while the request is in flight. */}
          {numbersQuery.isLoading ? (
            <Spinner label="Loading numbers" />
          ) : null}

          {/* A failed numbers request must say so. Without this branch the whole section
              would vanish silently and the user would have no way to add a verification. */}
          {numbersQuery.isError ? (
            <div role="alert" className="space-y-2">
              <p className="text-[12.5px] text-[hsl(var(--cx-danger))]">
                {getErrorMessage(numbersQuery.error)}
              </p>
              <Button
                type="button"
                variant="outline"
                onClick={() => void numbersQuery.refetch()}
              >
                Retry
              </Button>
            </div>
          ) : null}

          {!numbersQuery.isLoading && !numbersQuery.isError && numbersQuery.data != null ? (
            eligibleNumbers.length === 0 ? (
              <EmptyState
                title="No toll-free numbers available"
                description="Toll-free verification applies only to toll-free numbers (800/833/844/855/866/877/888), and this workspace has none available to verify right now."
              />
            ) : (
              <ConsoleCard>
                <form className="space-y-[11px]" onSubmit={handleCreate}>
                  <div>
                    <label htmlFor="tfv-number" className={LABEL_CLASS}>
                      Toll-free number
                    </label>
                    <Select
                      id="tfv-number"
                      required
                      value={numberId}
                      onChange={(event) => setNumberId(event.target.value)}
                    >
                      <option value="">Select a toll-free number</option>
                      {eligibleNumbers.map((number) => (
                        <option key={number.id} value={number.id}>
                          {number.e164}
                        </option>
                      ))}
                    </Select>
                  </div>

                  <div>
                    <label htmlFor="tfv-business-name" className={LABEL_CLASS}>
                      Business name
                    </label>
                    <Input
                      id="tfv-business-name"
                      required
                      maxLength={255}
                      value={businessName}
                      onChange={(event) => setBusinessName(event.target.value)}
                    />
                  </div>

                  <div>
                    <label htmlFor="tfv-use-case" className={LABEL_CLASS}>
                      Use case
                    </label>
                    <Select
                      id="tfv-use-case"
                      value={useCase}
                      onChange={(event) => setUseCase(event.target.value)}
                    >
                      {TFV_USE_CASES.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </Select>
                  </div>

                  <div>
                    <label htmlFor="tfv-use-case-summary" className={LABEL_CLASS}>
                      Use case summary
                    </label>
                    <Textarea
                      id="tfv-use-case-summary"
                      required
                      value={useCaseSummary}
                      onChange={(event) => setUseCaseSummary(event.target.value)}
                    />
                    <span className={HELP_CLASS}>
                      What the messages will say.
                    </span>
                  </div>

                  <div>
                    <label htmlFor="tfv-opt-in-process" className={LABEL_CLASS}>
                      Opt-in process
                    </label>
                    <Textarea
                      id="tfv-opt-in-process"
                      required
                      value={optInProcess}
                      onChange={(event) => setOptInProcess(event.target.value)}
                    />
                    <span className={HELP_CLASS}>
                      How subscribers consent to receive messages.
                    </span>
                  </div>

                  <div>
                    <label htmlFor="tfv-opt-in-screenshot-url" className={LABEL_CLASS}>
                      Opt-in screenshot URL
                    </label>
                    <Input
                      id="tfv-opt-in-screenshot-url"
                      type="url"
                      value={screenshotUrl}
                      onChange={(event) => setScreenshotUrl(event.target.value)}
                    />
                  </div>

                  <div>
                    <label htmlFor="tfv-message-volume" className={LABEL_CLASS}>
                      Monthly message volume
                    </label>
                    <Input
                      id="tfv-message-volume"
                      type="number"
                      min={0}
                      value={messageVolume}
                      onChange={(event) => setMessageVolume(event.target.value)}
                    />
                  </div>

                  <div>
                    <label htmlFor="tfv-contact-email" className={LABEL_CLASS}>
                      Contact email
                    </label>
                    <Input
                      id="tfv-contact-email"
                      type="email"
                      value={contactEmail}
                      onChange={(event) => setContactEmail(event.target.value)}
                    />
                  </div>

                  <p className="text-[11.5px] text-[hsl(var(--cx-muted))]">
                    Use case summary and opt-in process are required: the verification cannot
                    be submitted without them.
                  </p>

                  <Button type="submit" disabled={!canCreate || createMutation.isPending}>
                    Create verification
                  </Button>
                </form>
              </ConsoleCard>
            )
          ) : null}
        </div>
      ) : null}

      {!canManage ? (
        <p className="text-[12.5px] text-[hsl(var(--cx-muted))]">
          A workspace admin with the compliance permission makes changes here.
        </p>
      ) : null}

      <MutationStatus
        pending={submitMutation.isPending}
        error={submitMutation.error}
        success={
          submitMutation.isSuccess ? "Marked submitted in this workspace." : undefined
        }
        pendingLabel="Marking submitted…"
      />
      <MutationStatus
        pending={createMutation.isPending}
        error={createMutation.error}
        success={
          createMutation.isSuccess ? "Created in this workspace." : undefined
        }
        pendingLabel="Creating verification…"
      />
    </SurfaceCard>
  );
}
