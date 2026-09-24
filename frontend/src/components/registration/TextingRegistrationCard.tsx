import { useState, type FormEvent } from "react";

import { useGate } from "@/api/capabilities";
import { defaultCheckoutRedirect } from "@/api/billing";
import {
  useRegistrationBrands,
  useRegistrationCampaigns,
  useResendTextingOtp,
  useTextingCheckout,
  useTextingRegistration,
  useVerifyTextingOtp,
  type RegistrationBrand,
  type RegistrationCampaign,
  type TextingRegistration,
  type TextingRegistrationState,
  type TextingStage,
} from "@/api/registration";
import { useAuth } from "@/auth/AuthContext";
import { ConsoleCard, SectionLabel } from "@/components/ui/consoleChrome";
import {
  Button,
  Input,
  MutationStatus,
  Spinner,
  mutationErrorMessage,
} from "@/components/ui/primitives";

import { TextingRegistrationForm } from "./TextingRegistrationForm";

const MUTED = "text-[12px] text-[hsl(var(--cx-muted))]";
const DANGER = "text-[12px] text-[hsl(var(--cx-danger))]";

/** How far each stage that draws the progress list has travelled (otp_pending ranks 2). */
const STAGE_RANK: Partial<Record<TextingStage, number>> = {
  paid: 0,
  brand_filed: 1,
  otp_pending: 2,
  brand_approved: 3,
  campaign_filed: 4,
  active: 5,
};

function ErrorAlert({ error }: { error: unknown }): JSX.Element {
  return (
    <div role="alert" className={DANGER}>
      {mutationErrorMessage(error)}
    </div>
  );
}

/** The carrier PIN entry. It owns the typed code; the card owns the mutations. */
function OtpStep({
  canManage,
  pending,
  error,
  onVerify,
  resendPending,
  resendError,
  resendSuccess,
  onResend,
}: {
  canManage: boolean;
  pending: boolean;
  error: unknown;
  onVerify: (pin: string) => void;
  resendPending: boolean;
  resendError: unknown;
  resendSuccess: boolean;
  onResend: () => void;
}): JSX.Element {
  const [pin, setPin] = useState("");

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pin.length !== 6 || pending) return;
    onVerify(pin);
  }

  return (
    <>
      <p className={MUTED}>
        {"The carrier texted a 6-digit code to the owner's mobile. It is valid for 24 hours."}
      </p>
      {canManage ? (
        <>
          <form
            aria-label="Verify owner's mobile"
            noValidate
            className="flex flex-col gap-[9px]"
            onSubmit={handleSubmit}
          >
            <div className="flex flex-col gap-1">
              <label
                htmlFor="texting-otp"
                className="text-[11.5px] font-medium text-[hsl(var(--cx-muted))]"
              >
                Verification code
              </label>
              <Input
                id="texting-otp"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                value={pin}
                onChange={(event) => setPin(event.target.value.replace(/\D/g, "").slice(0, 6))}
              />
            </div>
            <div className="flex items-center gap-2">
              <Button type="submit" disabled={pin.length !== 6 || pending}>
                Verify
              </Button>
              <Button type="button" variant="outline" disabled={resendPending} onClick={onResend}>
                Send a new code
              </Button>
            </div>
          </form>
          <MutationStatus pending={pending} error={error} pendingLabel={"Checking code\u2026"} />
          <MutationStatus
            pending={resendPending}
            error={resendError}
            success={resendSuccess ? "A new code is on its way." : undefined}
            pendingLabel={"Sending\u2026"}
          />
        </>
      ) : null}
    </>
  );
}

/** The checklist for every stage after payment. */
function ProgressSteps({ registration }: { registration: TextingRegistration }): JSX.Element {
  const rank = STAGE_RANK[registration.stage] ?? 0;
  const detail = registration.detail ?? "";

  const steps: { label: string; done: boolean }[] = [
    { label: "Payment received", done: rank >= 0 },
    { label: "Business submitted to the carrier", done: rank >= 1 },
  ];
  if (registration.fee_tier === "sole_proprietor") {
    steps.push({ label: "Owner's mobile verified", done: rank >= 3 });
  }
  steps.push(
    { label: "Business approved", done: rank >= 3 },
    { label: "Campaign submitted", done: rank >= 4 },
    { label: "Carriers approved texting", done: rank >= 5 },
  );

  return (
    <>
      <ol aria-label="Texting registration progress" className="flex flex-col gap-[4px]">
        {steps.map((step) => (
          <li
            key={step.label}
            data-done={step.done ? "true" : "false"}
            className="flex items-center gap-2 text-[12.5px]"
          >
            <span aria-hidden="true">{step.done ? "\u2713" : "\u25cb"}</span>
            <span
              className={
                step.done ? "text-[hsl(var(--cx-text))]" : "text-[hsl(var(--cx-muted))]"
              }
            >
              {step.label}
            </span>
          </li>
        ))}
      </ol>
      {detail !== "" ? <p className={MUTED}>{detail}</p> : null}
      {registration.stage === "active" ? (
        <p className={MUTED}>
          Texting is live. Your numbers are being added to your campaign automatically.
        </p>
      ) : null}
    </>
  );
}

export function TextingRegistrationCard({
  onCheckout = defaultCheckoutRedirect,
}: { onCheckout?: (url: string) => void } = {}): JSX.Element {
  const { api } = useAuth();
  const gate = useGate();
  const canManage = gate.can("compliance:manage");

  const texting = useTextingRegistration(api);
  const brandsQuery = useRegistrationBrands(api);
  const campaignsQuery = useRegistrationCampaigns(api);
  const checkout = useTextingCheckout(api);
  const verify = useVerifyTextingOtp(api);
  const resend = useResendTextingOtp(api);

  function renderForm(
    data: TextingRegistrationState,
    registration: TextingRegistration | null,
  ): JSX.Element {
    const rejected =
      registration !== null &&
      (registration.stage === "brand_rejected" || registration.stage === "campaign_rejected");
    const detail = registration?.detail ?? "";
    const brands: RegistrationBrand[] = Array.isArray(brandsQuery.data) ? brandsQuery.data : [];
    const campaigns: RegistrationCampaign[] = Array.isArray(campaignsQuery.data)
      ? campaignsQuery.data
      : [];

    // Readers still see why the last attempt failed; only the form itself is manager-only.
    let body: JSX.Element;
    if (!canManage) {
      body = (
        <p className={MUTED}>Ask a workspace admin to register this workspace for texting.</p>
      );
    } else if (brandsQuery.isLoading || campaignsQuery.isLoading) {
      body = <Spinner label="Loading business profiles" />;
    } else if (brandsQuery.error) {
      body = <ErrorAlert error={brandsQuery.error} />;
    } else if (campaignsQuery.error) {
      body = <ErrorAlert error={campaignsQuery.error} />;
    } else {
      body = (
        <TextingRegistrationForm
          brands={brands}
          campaigns={campaigns}
          quotes={data.quotes}
          pending={checkout.isPending}
          error={checkout.error}
          onSubmit={(input) =>
            checkout.mutate(input, {
              onSuccess: (saved) => {
                if (saved.checkout_url) onCheckout(saved.checkout_url);
              },
            })
          }
        />
      );
    }

    return (
      <>
        {rejected && detail !== "" ? (
          <div role="alert" className={DANGER}>
            Your last attempt was not approved: {detail}
          </div>
        ) : null}
        {body}
      </>
    );
  }

  function renderStage(): JSX.Element {
    if (texting.isLoading) return <Spinner label="Loading texting registration" />;
    if (texting.error) return <ErrorAlert error={texting.error} />;

    const data = texting.data;
    if (data === undefined) return <Spinner label="Loading texting registration" />;

    const { registration } = data;
    if (registration === null) return renderForm(data, null);

    switch (registration.stage) {
      case "expired":
      case "brand_rejected":
      case "campaign_rejected":
        return renderForm(data, registration);
      case "checkout": {
        const checkoutUrl = registration.checkout_url;
        return (
          <>
            <p className={MUTED}>Your payment page is ready.</p>
            {canManage && checkoutUrl !== null ? (
              <Button type="button" onClick={() => onCheckout(checkoutUrl)}>
                Continue to payment
              </Button>
            ) : null}
          </>
        );
      }
      case "otp_pending":
        return (
          <OtpStep
            canManage={canManage}
            pending={verify.isPending}
            error={verify.error}
            onVerify={(pin) => verify.mutate({ registrationId: registration.id, pin })}
            resendPending={resend.isPending}
            resendError={resend.error}
            resendSuccess={resend.isSuccess}
            onResend={() => resend.mutate(registration.id)}
          />
        );
      case "paid":
      case "brand_filed":
      case "brand_approved":
      case "campaign_filed":
      case "active":
        return <ProgressSteps registration={registration} />;
      case "needs_attention":
        return (
          <div role="alert" className={`flex flex-col gap-[4px] ${DANGER}`}>
            {registration.detail ? <p>{registration.detail}</p> : null}
            <p>Our team has been notified.</p>
          </div>
        );
    }
  }

  return (
    <ConsoleCard className="flex flex-col gap-[9px]">
      <SectionLabel>Register for texting</SectionLabel>
      {renderStage()}
    </ConsoleCard>
  );
}
