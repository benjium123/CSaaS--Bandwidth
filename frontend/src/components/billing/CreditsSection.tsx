import { BalanceCard } from "./BalanceCard";
import { BillingLedger } from "./BillingLedger";
import { PaymentMethods } from "./PaymentMethods";
import { RateSheet } from "./RateSheet";
import { UsageTable } from "./UsageTable";
import { Collapsible } from "@/components/ui/primitives";

/**
 * The "Credits" tab of Settings -> Billing & usage.
 *
 * Pure composition: every child owns its own query, permission gate and empty state, so
 * this file exists only to fix the ORDER - what you have, what you spent it on, what it
 * costs, how you pay, and only then the raw log.
 *
 * The ledger is collapsed by default: it is reference material, not the answer to "how
 * much do I have left?", and an always-expanded ledger pushes the balance and the
 * Add-credits buttons off a laptop screen.
 *
 * PlatformBillingOps is deliberately NOT here - it belongs to /settings/developers
 * (PlatformPage), the operator surface, and must never sit next to a customer's balance.
 */
export function CreditsSection() {
  return (
    <div className="space-y-6">
      <BalanceCard />
      <UsageTable />
      <RateSheet />
      <PaymentMethods />
      <Collapsible storageKey="settings.billing.ledger" title="Credit history" defaultOpen={false}>
        <BillingLedger />
      </Collapsible>
    </div>
  );
}
