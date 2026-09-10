import { getErrorMessage } from "@/api/spend";
import {
  formatRateUnit,
  formatUnitPrice,
  metricLabel,
  rateDisplayMicros,
  useRates,
} from "@/api/billing";
import { useAuth } from "@/auth/AuthContext";
import { Button, EmptyState, Section, Spinner } from "@/components/ui/primitives";

export function RateSheet() {
  const { api } = useAuth();
  const ratesQuery = useRates(api);
  const rates = ratesQuery.data;

  return (
    <Section title="Prices" description="What your credits buy.">
      {ratesQuery.isLoading ? <Spinner label="Loading prices" /> : null}

      {ratesQuery.isError ? (
        <div role="alert">
          <p className="text-sm text-destructive">{getErrorMessage(ratesQuery.error)}</p>
          <Button type="button" variant="outline" onClick={() => void ratesQuery.refetch()}>
            Retry
          </Button>
        </div>
      ) : null}

      {rates != null ? (
        rates.length === 0 ? (
          <EmptyState title="Prices are not available yet." />
        ) : (
          <>
            <table className="w-full text-sm">
              <caption className="sr-only">Prices</caption>
              <thead>
                <tr>
                  <th className="text-left font-medium">What you use</th>
                  <th className="text-right font-medium">Price</th>
                </tr>
              </thead>
              <tbody>
                {/* Keyed by position as well as metric: the customer view is MEANT to send
                    one row per metric, but if a backend ever sends two rows for the same
                    one (say a per-provider price), a metric-only key would collide and
                    React would drop a row silently instead of showing both. */}
                {rates.map((row, index) => (
                  <tr key={`${row.metric}-${index}`}>
                    <td>{metricLabel(row.metric)}</td>
                    <td className="text-right">
                      {`${formatUnitPrice(rateDisplayMicros(row.metric, row.price_micros))} ${
                        row.unit ?? formatRateUnit(row.metric)
                      }`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-3 text-sm text-muted-foreground">
              Prices are per unit and are taken from your credits as you use them.
            </p>
          </>
        )
      ) : null}
    </Section>
  );
}
