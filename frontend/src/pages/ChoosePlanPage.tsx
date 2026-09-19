import { defaultCheckoutRedirect } from "@/api/billing";
import { useAuth } from "@/auth/AuthContext";
import {
  includedLines,
  isPurchasable,
  usePlans,
  useSubscriptionCheckout,
} from "@/api/plans";
import {
  AuthAlert,
  AuthButton,
  AuthNotice,
  AuthSurface,
  Lamp,
} from "@/components/auth/AuthShell";
import "@/pages/choosePlan.css";

const NO_MESSAGE = "The request failed and no message came back.";
const quantityFormatter = new Intl.NumberFormat("en-US");

function PlansSkeleton() {
  return (
    <AuthSurface>
      <div className="cp-page">
        <div className="cp-skeleton" aria-busy="true" aria-label="Loading plans">
          <span className="cp-skeleton-card" />
          <span className="cp-skeleton-card" />
          <span className="cp-skeleton-card" />
        </div>
      </div>
    </AuthSurface>
  );
}

/**
 * Choose a subscription plan.
 *
 * The rules this page exists to keep:
 * 1. Never render an invented price. Zero price micros is a placeholder, not "free".
 * 2. Never claim anything from an unloaded resource. [] before load is not absence.
 * 3. Render the server's error verbatim, never substitute friendlier copy.
 * 4. Every interactive element is a real button whose accessible name is its visible text.
 */
export function ChoosePlanPage({
  onCheckout = defaultCheckoutRedirect,
}: { onCheckout?: (url: string) => void } = {}) {
  const { api } = useAuth();
  const plansQuery = usePlans(api);
  const checkout = useSubscriptionCheckout(api, onCheckout);

  if (plansQuery.isPending) {
    return <PlansSkeleton />;
  }

  if (plansQuery.isError) {
    return (
      <AuthSurface>
        <div className="cp-page">
          <AuthAlert>
            {plansQuery.error instanceof Error ? plansQuery.error.message : NO_MESSAGE}
          </AuthAlert>
        </div>
      </AuthSurface>
    );
  }

  if (plansQuery.data === undefined) {
    return <PlansSkeleton />;
  }

  const plans = plansQuery.data;

  return (
    <AuthSurface>
      <div className="cp-page">
        <header className="cp-head">
          <div className="ex-label">Billing</div>
          <h1 className="cp-title ex-nameplate">Choose your plan</h1>
          <p className="cp-lede">Pick the plan that fits how your team calls and texts.</p>
        </header>

        {plansQuery.isSuccess && plans.length === 0 ? (
          <div className="cp-empty">
            {/* Only a SUCCESSFUL response may say there are no plans; [] before load is not
                evidence of absence. */}
            Your workspace has no plans to choose from. If you were expecting one, contact support.
          </div>
        ) : (
          <>
            <ul className="cp-plans">
              {plans.map((plan) => {
                const purchasable = isPurchasable(plan);
                const headingId = `plan-${plan.code}`;
                const noticeId = `plan-${plan.code}-notice`;

                return (
                  <li key={plan.code} className="cp-plan">
                    <section className="cp-plan-body" aria-labelledby={headingId}>
                      <h2 id={headingId} className="cp-plan-name">
                        {plan.name}
                      </h2>
                      <div className="ex-label">Included</div>
                      <ul className="cp-included">
                        {includedLines(plan).map((line) => (
                          <li key={line.key}>
                            <span>{line.label}</span>
                            <b>{quantityFormatter.format(line.quantity)}</b>
                          </li>
                        ))}
                      </ul>

                      {purchasable ? (
                        <AuthButton
                          type="button"
                          block
                          disabled={checkout.isPending}
                          onClick={() => checkout.mutate({ plan_code: plan.code })}
                        >
                          Choose {plan.name}
                        </AuthButton>
                      ) : (
                        <>
                          <p id={noticeId} className="cp-unavailable-copy">
                            Pricing for this plan is not published yet. You cannot subscribe to it today.
                          </p>
                          <AuthButton
                            type="button"
                            block
                            disabled
                            aria-describedby={noticeId}
                          >
                            Not yet available
                          </AuthButton>
                        </>
                      )}
                    </section>
                  </li>
                );
              })}
            </ul>

            <AuthNotice>
              Subscriptions are billed through Stripe and can be changed later.
            </AuthNotice>

            {checkout.isPending && (
              <div className="cp-checkout-lamp">
                <Lamp state="wait">Opening secure checkout</Lamp>
              </div>
            )}

            {checkout.isError && (
              <AuthAlert>
                {checkout.error instanceof Error ? checkout.error.message : NO_MESSAGE}
              </AuthAlert>
            )}
          </>
        )}
      </div>
    </AuthSurface>
  );
}
