import { useState } from "react";
import { getErrorMessage } from "@/api/spend";
import { useGate } from "@/api/capabilities";
import { useAuth } from "@/auth/AuthContext";
import {
  defaultCheckoutRedirect,
  useAddPaymentMethod,
  useDeletePaymentMethod,
  usePaymentMethods,
} from "@/api/billing";
import {
  Button,
  Card,
  EmptyState,
  MutationStatus,
  Pill,
  Section,
  Spinner,
} from "@/components/ui/primitives";

export function PaymentMethods({
  onCheckout = defaultCheckoutRedirect,
}: {
  onCheckout?: (url: string) => void;
}) {
  const { api } = useAuth();
  const gate = useGate();
  const canManage = gate.can("org:billing");

  const methodsQuery = usePaymentMethods(api);
  const deleteMutation = useDeletePaymentMethod(api);
  const addMutation = useAddPaymentMethod(api);

  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  function handleAddCard() {
    addMutation.mutate(undefined, {
      onSuccess: (result) => onCheckout(result.checkout_url),
    });
  }

  return (
    <Section
      title="Cards"
      actions={
        methodsQuery.data != null && methodsQuery.data.length > 0 ? (
          <Button
            type="button"
            variant="outline"
            disabled={!canManage || addMutation.isPending}
            onClick={handleAddCard}
          >
            Add a card
          </Button>
        ) : undefined
      }
    >
      {!canManage ? (
        <p className="text-sm text-muted-foreground">
          Only the workspace owner can change cards.
        </p>
      ) : null}

      {methodsQuery.isLoading ? <Spinner label="Loading cards" /> : null}

      {methodsQuery.isError ? (
        <div role="alert">
          <p className="text-sm text-destructive">{getErrorMessage(methodsQuery.error)}</p>
          <Button type="button" variant="outline" onClick={() => void methodsQuery.refetch()}>
            Retry
          </Button>
        </div>
      ) : null}

      {methodsQuery.data != null ? (
        methodsQuery.data.length === 0 ? (
          <EmptyState
            title="No card saved."
            description="Add a card to top up automatically."
            action={
              <Button
                type="button"
                disabled={!canManage || addMutation.isPending}
                onClick={handleAddCard}
              >
                Add a card
              </Button>
            }
          />
        ) : (
          <div className="space-y-3">
            {methodsQuery.data.map((method) => (
              <Card key={method.id}>
                <div className="flex items-center justify-between gap-2">
                  <div className="text-sm font-medium">
                    {method.brand} ending {method.last4}
                  </div>
                  {method.is_default ? <Pill tone="success">Default</Pill> : null}
                </div>

                {method.exp_month != null && method.exp_year != null ? (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Expires {method.exp_month}/{method.exp_year}
                  </p>
                ) : null}

                <div className="mt-3 flex items-center gap-2">
                  {confirmingId === method.id ? (
                    <>
                      <Button
                        type="button"
                        variant="outline"
                        aria-label={`Confirm removing ${method.brand} ending ${method.last4}`}
                        disabled={!canManage || deleteMutation.isPending}
                        onClick={() => {
                          deleteMutation.mutate(method.id, {
                            onSuccess: () => setConfirmingId(null),
                          });
                        }}
                      >
                        Confirm remove
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        aria-label={`Cancel removing ${method.brand} ending ${method.last4}`}
                        disabled={!canManage || deleteMutation.isPending}
                        onClick={() => setConfirmingId(null)}
                      >
                        Cancel
                      </Button>
                    </>
                  ) : (
                    <Button
                      type="button"
                      variant="outline"
                      aria-label={`Remove ${method.brand} ending ${method.last4}`}
                      disabled={!canManage || deleteMutation.isPending}
                      onClick={() => setConfirmingId(method.id)}
                    >
                      Remove
                    </Button>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )
      ) : null}

      <MutationStatus
        pending={deleteMutation.isPending}
        error={deleteMutation.error}
        pendingLabel="Removing card…"
      />
      <MutationStatus
        pending={addMutation.isPending}
        error={addMutation.error}
        pendingLabel="Adding card…"
      />
    </Section>
  );
}
