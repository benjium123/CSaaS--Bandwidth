import { useState } from "react";
import { isOwner, useAuth } from "@/auth/AuthContext";
import { Button, Card, CardHeader, Spinner, mutationErrorMessage } from "@/components/ui/primitives";
import { dollars, useChangePlan, useTrimPlan, useWorkspacePlan } from "@/api/plan";
import type { CatalogPlan } from "@/api/plan";

/** Settings > Billing: the workspace plan, what it includes, and moving between plans. */
export function PlanCard() {
  const { api, me, orgId } = useAuth();
  const owner = isOwner(me, orgId);
  const planQ = useWorkspacePlan(api, owner);
  const change = useChangePlan(api);
  const trim = useTrimPlan(api);
  const [confirming, setConfirming] = useState<CatalogPlan | null>(null);

  if (!owner) return null;
  if (planQ.isPending) return <Card><Spinner label="Loading plan" /></Card>;
  if (planQ.isError) {
    return <Card><p role="alert" className="text-sm text-destructive">{mutationErrorMessage(planQ.error)}</p></Card>;
  }
  const data = planQ.data;
  const plan = data.plan;
  if (!plan) {
    return (
      <Card>
        <CardHeader title="Plan" description="Choose Solo, Team or Business when you pick your first phone numbers." />
        <a href="/choose-numbers" className="mt-3 inline-block text-sm font-medium text-primary underline">Choose a plan</a>
      </Card>
    );
  }
  const spareUsers = (data.users.limit ?? 0) - data.users.in_use;
  const spareNumbers = (data.numbers.limit ?? 0) - data.numbers.in_use;
  const trimmable = (data.users.extra ?? 0) > 0 && spareUsers > 0 || (data.numbers.extra ?? 0) > 0 && spareNumbers > 0;

  return (
    <Card>
      <CardHeader
        title={`${plan.name} plan`}
        description={`${dollars(plan.monthly_total_cents)} a month${plan.renews_at ? `, renews ${new Date(plan.renews_at).toLocaleDateString()}` : ""}${plan.status === "past_due" ? " - payment overdue, update your card" : ""}`}
      />
      <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-muted-foreground">Users</dt>
          <dd className="font-medium">{data.users.in_use} of {data.users.limit}</dd>
          <dd className="text-xs text-muted-foreground">{data.users.included} included{data.users.extra ? ` + ${data.users.extra} × ${dollars(data.extra_user_cents)}` : ""}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Phone numbers</dt>
          <dd className="font-medium">{data.numbers.in_use} of {data.numbers.limit}</dd>
          <dd className="text-xs text-muted-foreground">{data.numbers.included} included{data.numbers.extra ? ` + ${data.numbers.extra} × ${dollars(data.extra_number_cents)}` : ""}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Call minutes this month</dt>
          <dd className="font-medium">{data.minutes?.remaining ?? 0} of {data.minutes?.included ?? 0} left</dd>
          <dd className="text-xs text-muted-foreground">Shared by the whole team. Then credit.</dd>
        </div>
      </dl>

      <div className="mt-5 grid gap-2 sm:grid-cols-3">
        {data.catalog.map(p => {
          const isCurrent = p.code === plan.code;
          return (
            <div key={p.code} className="rounded-xl border border-[hsl(var(--cx-line))] p-3" data-current={isCurrent || undefined}>
              <p className="text-sm font-semibold">{p.name} · {dollars(p.price_cents)}/mo</p>
              <p className="text-xs text-muted-foreground">{p.users} users · {p.numbers} numbers · {p.minutes} min</p>
              {isCurrent ? (
                <p className="mt-2 text-xs font-medium text-[hsl(var(--cx-live))]">Your plan</p>
              ) : (
                <Button type="button" variant="outline" size="sm" className="mt-2 rounded-full" onClick={() => setConfirming(p)}>
                  Switch · {dollars(p.monthly_total_cents_if_switched)}/mo
                </Button>
              )}
            </div>
          );
        })}
      </div>

      {confirming && (
        <div role="region" aria-label="Confirm plan change" className="mt-4 space-y-2 rounded-xl border border-[hsl(var(--cx-line))] p-3">
          <p className="text-sm">
            Move to <b>{confirming.name}</b>: your bill becomes <b>{dollars(confirming.monthly_total_cents_if_switched)}/month</b>, keeping
            everyone and every number you have now. The difference for the rest of this month is charged or credited today.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              size="sm"
              className="rounded-full"
              disabled={change.isPending}
              onClick={() => change.mutate(
                { plan_code: confirming.code, accept_cents: confirming.monthly_total_cents_if_switched },
                { onSuccess: () => setConfirming(null) },
              )}
            >
              {change.isPending ? "Switching…" : `Switch to ${confirming.name}`}
            </Button>
            <Button type="button" size="sm" variant="outline" className="rounded-full" onClick={() => setConfirming(null)}>Cancel</Button>
          </div>
          {change.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(change.error)}</p>}
        </div>
      )}

      {trimmable && (
        <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
          <span className="text-muted-foreground">You are paying for add-ons nobody is using.</span>
          <Button type="button" size="sm" variant="outline" className="rounded-full" disabled={trim.isPending} onClick={() => trim.mutate({})}>
            Stop paying for unused add-ons
          </Button>
          {trim.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(trim.error)}</p>}
        </div>
      )}
    </Card>
  );
}
