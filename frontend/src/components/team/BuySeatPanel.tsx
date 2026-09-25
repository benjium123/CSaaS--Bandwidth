import { isOwner, useAuth } from "@/auth/AuthContext";
import { dollars, useAddPlanUsers, useWorkspacePlan } from "@/api/plan";
import { Button, mutationErrorMessage } from "@/components/ui/primitives";

/**
 * Every user on the plan is taken. The owner can buy another ($15/month, charged now and
 * prorated) and carry straight on adding the teammate; anyone else is told who can.
 */
export function BuySeatPanel({ onBought, onClose }: { onBought: () => void; onClose: () => void }) {
  const { api, me, orgId } = useAuth();
  const owner = isOwner(me, orgId);
  const plan = useWorkspacePlan(api, owner);
  const add = useAddPlanUsers(api);
  const userCents = plan.data?.extra_user_cents ?? 1500;
  const numberCents = plan.data?.extra_number_cents ?? 500;
  const current = plan.data?.plan;
  const freeNumbers = plan.data?.numbers.limit != null ? Math.max(plan.data.numbers.limit - plan.data.numbers.in_use, 0) : 0;

  return (
    <section
      role="region"
      aria-label="Add a user"
      className="space-y-3 rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] p-4"
    >
      {!owner ? (
        <p className="text-[13px] text-muted-foreground">
          Every user on your plan is in use. Ask an owner to add a user ({dollars(userCents)}/month).
        </p>
      ) : plan.isPending ? (
        <p className="text-[13px] text-muted-foreground">Loading your plan…</p>
      ) : !current ? (
        <p className="text-[13px] text-muted-foreground">
          Choose a plan to add people.{" "}
          <a href="/choose-numbers" className="font-medium text-primary underline">Choose a plan</a>
        </p>
      ) : (
        <>
          <div>
            <p className="text-[14px] font-semibold">
              All {plan.data!.users.limit} users on your {current.name} plan are in use
            </p>
            <p className="mt-1 text-[13px] leading-relaxed text-muted-foreground">
              Add another user for {dollars(userCents)}/month, charged now for the rest of this month.
              {freeNumbers > 0
                ? ` Your plan still has ${freeNumbers} free number${freeNumbers === 1 ? "" : "s"} to give them.`
                : ` A number for them is ${dollars(numberCents)}/month more.`}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              className="rounded-full px-5"
              disabled={add.isPending}
              onClick={() =>
                add.mutate({ count: 1, accept_cents: userCents }, { onSuccess: () => onBought() })
              }
            >
              {add.isPending ? "Adding…" : `Add a user for ${dollars(userCents)}/month`}
            </Button>
            <Button type="button" variant="outline" className="rounded-full px-5" onClick={onClose}>
              Cancel
            </Button>
          </div>
          {add.isError && (
            <p role="alert" className="text-[13px] text-destructive">{mutationErrorMessage(add.error)}</p>
          )}
        </>
      )}
    </section>
  );
}
