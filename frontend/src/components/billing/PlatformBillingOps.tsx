import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { dollarsToMicros, getErrorMessage, lastNDaysRange, microsToDollars } from "@/api/spend";
import { formatCredits } from "@/api/billing";
import type { ApiClient } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import {
  Button,
  Card,
  Collapsible,
  EmptyState,
  Input,
  MutationStatus,
  Section,
  Select,
  Spinner,
} from "@/components/ui/primitives";

// No global billing-settings endpoint and no org-list endpoint exist on the backend
// (backend/app/api/routes/platform.py only has GET/PATCH by org_id, POST adjustments
// nested under org_id, PUT rates, GET margin) - the ops panel below works from a single
// operator-entered workspace id rather than browsing a list. See docs/OPEN_ISSUES.md D54
// for the follow-up: a real paginated org-search endpoint.
export const OPS_RATES_PATH = "/api/v1/platform/billing/rates";
export const OPS_ORGS_PATH = "/api/v1/platform/billing/orgs";
export const OPS_MARGIN_PATH = "/api/v1/platform/billing/margin";

export interface OpsRate {
  provider: string;
  metric: string;
  cost_micros: number;
  price_micros: number;
}

export interface OpsOrg {
  org_id: string;
  name: string;
  ai_markup_bps: number | null;
  ai_platform_fee_per_minute_micros: number | null;
  balance_micros: number;
  telephony_prepaid?: boolean;
  telephony_prepaid_since?: string | null;
}

export interface MarginDay {
  day: string;
  price_micros: number;
  cost_micros: number;
  margin_micros: number;
}

export interface MarginReport {
  items: MarginDay[];
}

const TOKEN_STORAGE_KEY = "csaas.platform.ops.token";

type RateDraft = { cost: string; price: string };
type OrgOverrideDraft = { margin: string; fee: string };

function readStoredOpsToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function writeStoredOpsToken(token: string): void {
  try {
    if (token === "") sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    else sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    // Private mode / storage disabled - the token simply won't persist across reloads.
  }
}

/**
 * 403 is listed first on purpose: a rejected ops token SHOULD come back as 403, because
 * ApiClient turns any 401 into a whole-app logout (clearStoredAuth + onUnauthorized), so a
 * 401 here would sign the operator out of the console for mistyping a token. 401 is still
 * accepted so the section degrades gracefully if the backend does return one.
 */
function isUnauthorizedError(error: unknown): boolean {
  const status = (error as { status?: number } | null)?.status;
  if (status === 403 || status === 401) return true;
  return error instanceof Error && (error.message.includes("403") || error.message.includes("401"));
}

function rateKey(provider: string, metric: string): string {
  return `${provider}:${metric}`;
}

async function opsRequest<T>(
  api: ApiClient,
  token: string,
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const existingHeaders = (init.headers as Record<string, string> | undefined) ?? {};
  const headers: Record<string, string> = {
    ...existingHeaders,
    "X-Platform-Ops-Token": token,
  };
  return api.request<T>(path, { ...init, headers, json: init.json });
}

export function PlatformBillingOps() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [token, setToken] = React.useState<string>(readStoredOpsToken);
  const [inputValue, setInputValue] = React.useState("");
  const unlocked = token !== "";

  const marginRange = React.useMemo(() => lastNDaysRange(30), []);
  const marginPath = `${OPS_MARGIN_PATH}?from=${marginRange.from}&to=${marginRange.to}`;

  const ratesQuery = useQuery({
    queryKey: ["platform-billing", "rates", token],
    queryFn: () => opsRequest<OpsRate[]>(api, token, OPS_RATES_PATH),
    enabled: unlocked,
  });

  const marginQuery = useQuery({
    // The range is part of the key: a session that stays open across UTC midnight must
    // refetch rather than serve yesterday's 30-day window from cache.
    queryKey: ["platform-billing", "margin", token, marginRange.from, marginRange.to],
    queryFn: () => opsRequest<MarginReport>(api, token, marginPath),
    enabled: unlocked,
  });

  const [rateDrafts, setRateDrafts] = React.useState<Record<string, RateDraft>>({});

  // The same workspace id drives both the lookup card (balance/margin/fee display +
  // edit) and the adjustment form below it - there is no org-list endpoint to pick from.
  const [adjustOrgId, setAdjustOrgId] = React.useState("");
  const [lookupOrgId, setLookupOrgId] = React.useState("");
  const [orgDraft, setOrgDraft] = React.useState<OrgOverrideDraft>({ margin: "", fee: "" });
  const [adjustKind, setAdjustKind] = React.useState<"adjustment" | "refund">("adjustment");
  const [adjustAmount, setAdjustAmount] = React.useState("");
  const [adjustReason, setAdjustReason] = React.useState("");

  const orgQuery = useQuery({
    queryKey: ["platform-billing", "org", token, lookupOrgId],
    queryFn: () => opsRequest<OpsOrg>(api, token, `${OPS_ORGS_PATH}/${lookupOrgId}`),
    enabled: unlocked && lookupOrgId !== "",
  });

  function orgMarginBase(org: OpsOrg): string {
    return org.ai_markup_bps == null ? "" : String(org.ai_markup_bps / 100);
  }

  function orgFeeBase(org: OpsOrg): string {
    return org.ai_platform_fee_per_minute_micros == null
      ? ""
      : String(microsToDollars(org.ai_platform_fee_per_minute_micros));
  }

  React.useEffect(() => {
    if (orgQuery.data == null) return;
    setOrgDraft({ margin: orgMarginBase(orgQuery.data), fee: orgFeeBase(orgQuery.data) });
  }, [orgQuery.data]);

  const changedRates = React.useMemo(() => {
    return (ratesQuery.data ?? []).flatMap((row) => {
      const draft = rateDrafts[rateKey(row.provider, row.metric)];
      if (draft == null) return [];
      const cost = Number(draft.cost);
      const price = Number(draft.price);
      if (!Number.isFinite(cost) || !Number.isFinite(price) || cost < 0 || price < 0) return [];
      const costMicros = dollarsToMicros(cost);
      const priceMicros = dollarsToMicros(price);
      if (costMicros === row.cost_micros && priceMicros === row.price_micros) return [];
      return [{ provider: row.provider, metric: row.metric, cost_micros: costMicros, price_micros: priceMicros }];
    });
  }, [rateDrafts, ratesQuery.data]);

  const isRatesDirty = changedRates.length > 0;

  const hasInvalidRateDraft = React.useMemo(() => {
    return (ratesQuery.data ?? []).some((row) => {
      const draft = rateDrafts[rateKey(row.provider, row.metric)];
      if (draft == null) return false;
      const cost = Number(draft.cost);
      const price = Number(draft.price);
      return draft.cost.trim() === "" || draft.price.trim() === "" || !Number.isFinite(cost) || !Number.isFinite(price) || cost < 0 || price < 0;
    });
  }, [rateDrafts, ratesQuery.data]);

  React.useEffect(() => {
    if (isRatesDirty) return;
    const next: Record<string, RateDraft> = {};
    (ratesQuery.data ?? []).forEach((row) => {
      next[rateKey(row.provider, row.metric)] = {
        cost: String(microsToDollars(row.cost_micros)),
        price: String(microsToDollars(row.price_micros)),
      };
    });
    setRateDrafts(next);
  }, [ratesQuery.data, isRatesDirty]);

  const updateRatesMutation = useMutation({
    mutationFn: (rates: { provider: string; metric: string; cost_micros: number; price_micros: number }[]) =>
      opsRequest<OpsRate[]>(api, token, OPS_RATES_PATH, { method: "PUT", json: { rates } }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["platform-billing"], exact: false });
    },
  });

  const updateOrgMutation = useMutation({
    mutationFn: (input: {
      orgId: string;
      ai_markup_bps: number | null;
      ai_platform_fee_per_minute_micros: number | null;
    }) =>
      opsRequest<OpsOrg>(api, token, `${OPS_ORGS_PATH}/${input.orgId}`, {
        method: "PATCH",
        json: {
          ai_markup_bps: input.ai_markup_bps,
          ai_platform_fee_per_minute_micros: input.ai_platform_fee_per_minute_micros,
        },
      }),
    onSuccess: (org) => {
      void queryClient.invalidateQueries({ queryKey: ["platform-billing"], exact: false });
      setOrgDraft({ margin: orgMarginBase(org), fee: orgFeeBase(org) });
    },
  });

  // The per-workspace prepaid hard gate: when on, texting, outbound calling and number
  // orders draw from this balance and stop when it cannot cover them.
  const prepaidMutation = useMutation({
    mutationFn: (input: { orgId: string; telephony_prepaid: boolean }) =>
      opsRequest<OpsOrg>(api, token, `${OPS_ORGS_PATH}/${input.orgId}`, {
        method: "PATCH",
        json: { telephony_prepaid: input.telephony_prepaid },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["platform-billing"], exact: false });
    },
  });

  const adjustmentMutation = useMutation({
    mutationFn: (input: {
      org_id: string;
      entry_type: "adjustment" | "refund";
      amount_micros: number;
      note: string;
    }) =>
      opsRequest<unknown>(api, token, `${OPS_ORGS_PATH}/${input.org_id}/adjustments`, {
        method: "POST",
        json: input,
      }),
    onSuccess: () => {
      setAdjustAmount("");
      setAdjustReason("");
      void queryClient.invalidateQueries({ queryKey: ["platform-billing"], exact: false });
    },
  });

  const unauthorized = [ratesQuery.error, orgQuery.error, marginQuery.error].find(isUnauthorizedError);

  function handleUnlock(e: React.FormEvent) {
    e.preventDefault();
    const next = inputValue.trim();
    if (!next) return;
    setToken(next);
    writeStoredOpsToken(next);
    setInputValue("");
  }

  function handleLock() {
    setToken("");
    writeStoredOpsToken("");
    setInputValue("");
  }

  function handleRatesSave() {
    if (changedRates.length === 0 || hasInvalidRateDraft) return;
    updateRatesMutation.mutate(changedRates);
  }

  function handleLookupSubmit(e: React.FormEvent) {
    e.preventDefault();
    const id = adjustOrgId.trim();
    if (id === "") return;
    setLookupOrgId(id);
  }

  function handleOrgSave() {
    if (orgQuery.data == null) return;
    const marginEmpty = orgDraft.margin.trim() === "";
    const feeEmpty = orgDraft.fee.trim() === "";
    const marginInvalid = !marginEmpty && !Number.isFinite(Number(orgDraft.margin));
    const feeInvalid = !feeEmpty && !Number.isFinite(Number(orgDraft.fee));
    if (marginInvalid || feeInvalid) return;
    updateOrgMutation.mutate({
      orgId: lookupOrgId,
      ai_markup_bps: marginEmpty ? null : Math.round(Number(orgDraft.margin) * 100),
      ai_platform_fee_per_minute_micros: feeEmpty ? null : dollarsToMicros(Number(orgDraft.fee)),
    });
  }

  function handleAdjustmentSubmit(e: React.FormEvent) {
    e.preventDefault();
    const amount = Number(adjustAmount);
    if (
      adjustOrgId.trim() === "" ||
      adjustAmount.trim() === "" ||
      adjustReason.trim() === "" ||
      !Number.isFinite(amount)
    ) {
      return;
    }
    adjustmentMutation.mutate({
      org_id: adjustOrgId.trim(),
      entry_type: adjustKind,
      amount_micros: dollarsToMicros(amount),
      note: adjustReason.trim(),
    });
  }

  const marginItems = marginQuery.data?.items ?? [];
  const priceTotal = marginItems.reduce((sum, day) => sum + day.price_micros, 0);
  const costTotal = marginItems.reduce((sum, day) => sum + day.cost_micros, 0);
  const marginTotal = marginItems.reduce((sum, day) => sum + day.margin_micros, 0);

  const orgMarginInvalid = orgDraft.margin.trim() !== "" && !Number.isFinite(Number(orgDraft.margin));
  const orgFeeInvalid = orgDraft.fee.trim() !== "" && !Number.isFinite(Number(orgDraft.fee));
  const orgHasChanges =
    orgQuery.data != null &&
    (orgDraft.margin !== orgMarginBase(orgQuery.data) || orgDraft.fee !== orgFeeBase(orgQuery.data));

  return (
    <Collapsible
      storageKey="settings.platform.billing-ops"
      title="Platform operations"
      defaultOpen={false}
    >
      {unlocked ? (
        <div className="space-y-4">
          <div className="flex justify-end">
            <Button type="button" variant="outline" onClick={handleLock}>
              Lock
            </Button>
          </div>

          {unauthorized ? (
            <div role="alert" className="text-sm text-destructive">
              That token was not accepted.
            </div>
          ) : (
            <div className="space-y-8">
              <Section
                title="AI cost rates"
                description="What the platform pays and what it charges for AI usage."
              >
                {ratesQuery.isLoading ? (
                  <Spinner label="Loading rates" />
                ) : ratesQuery.isError ? (
                  <div role="alert" className="space-y-2 text-sm text-destructive">
                    <p>{getErrorMessage(ratesQuery.error)}</p>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => void ratesQuery.refetch()}
                    >
                      Retry
                    </Button>
                  </div>
                ) : (ratesQuery.data ?? []).length === 0 ? (
                  <EmptyState title="No AI cost rates yet." description="Add rates to bill AI usage." />
                ) : (
                  <div className="space-y-3">
                    <Card className="overflow-x-auto p-0">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="border-b border-border text-left text-xs text-muted-foreground">
                            <th className="px-3 py-2 font-medium">Provider</th>
                            <th className="px-3 py-2 font-medium">Metric</th>
                            <th className="px-3 py-2 font-medium">Cost</th>
                            <th className="px-3 py-2 font-medium">Price</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-border">
                          {(ratesQuery.data ?? []).map((row) => {
                            const key = rateKey(row.provider, row.metric);
                            const existing = rateDrafts[key] ?? {
                              cost: String(microsToDollars(row.cost_micros)),
                              price: String(microsToDollars(row.price_micros)),
                            };
                            return (
                              <tr key={key}>
                                <td className="px-3 py-2">{row.provider}</td>
                                <td className="px-3 py-2 font-mono text-xs">{row.metric}</td>
                                <td className="px-3 py-2">
                                  <Input
                                    aria-label={`${row.provider} ${row.metric} cost`}
                                    inputMode="decimal"
                                    value={existing.cost}
                                    onChange={(e) => {
                                      setRateDrafts((prev) => {
                                        const current = prev[key] ?? {
                                          cost: String(microsToDollars(row.cost_micros)),
                                          price: String(microsToDollars(row.price_micros)),
                                        };
                                        return { ...prev, [key]: { ...current, cost: e.target.value } };
                                      });
                                    }}
                                  />
                                </td>
                                <td className="px-3 py-2">
                                  <Input
                                    aria-label={`${row.provider} ${row.metric} price`}
                                    inputMode="decimal"
                                    value={existing.price}
                                    onChange={(e) => {
                                      setRateDrafts((prev) => {
                                        const current = prev[key] ?? {
                                          cost: String(microsToDollars(row.cost_micros)),
                                          price: String(microsToDollars(row.price_micros)),
                                        };
                                        return { ...prev, [key]: { ...current, price: e.target.value } };
                                      });
                                    }}
                                  />
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </Card>
                    <div className="flex items-center gap-3">
                      <Button
                        type="button"
                        onClick={handleRatesSave}
                        disabled={!isRatesDirty || hasInvalidRateDraft || updateRatesMutation.isPending}
                      >
                        Save rates
                      </Button>
                      <MutationStatus
                        pending={updateRatesMutation.isPending}
                        error={updateRatesMutation.error}
                        success={updateRatesMutation.isSuccess ? "Saved" : undefined}
                      />
                    </div>
                  </div>
                )}
              </Section>

              <Section
                title="Workspace"
                description="Look up a workspace by id - there is no directory to browse yet."
              >
                <Card className="space-y-3">
                  <form className="flex items-end gap-2" onSubmit={handleLookupSubmit}>
                    <div className="flex-1 space-y-1">
                      <label className="block text-xs text-muted-foreground" htmlFor="adjust-workspace">
                        Workspace id
                      </label>
                      <Input
                        id="adjust-workspace"
                        aria-label="Workspace id"
                        value={adjustOrgId}
                        onChange={(e) => setAdjustOrgId(e.target.value)}
                      />
                    </div>
                    <Button type="submit" disabled={adjustOrgId.trim() === ""}>
                      Load
                    </Button>
                  </form>

                  {lookupOrgId === "" ? null : orgQuery.isLoading ? (
                    <Spinner label="Loading workspace" />
                  ) : orgQuery.isError ? (
                    <div role="alert" className="space-y-2 text-sm text-destructive">
                      <p>{getErrorMessage(orgQuery.error)}</p>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        onClick={() => void orgQuery.refetch()}
                      >
                        Retry
                      </Button>
                    </div>
                  ) : orgQuery.data ? (
                    <div className="space-y-3 border-t border-border pt-3">
                      <div className="text-sm">
                        <span className="text-muted-foreground">Balance: </span>
                        {formatCredits(orgQuery.data.balance_micros)}
                      </div>
                      <div className="space-y-1">
                        <label className="flex items-center gap-2 text-sm">
                          <input
                            type="checkbox"
                            aria-label="Prepaid telephony"
                            checked={Boolean(orgQuery.data.telephony_prepaid)}
                            disabled={prepaidMutation.isPending}
                            onChange={(e) =>
                              prepaidMutation.mutate({
                                orgId: lookupOrgId,
                                telephony_prepaid: e.target.checked,
                              })
                            }
                          />
                          Prepaid texting and calling
                        </label>
                        <p className="text-xs text-muted-foreground">
                          When on, texts, outbound calls and new numbers draw from this
                          balance and stop when it runs out. Inbound is still charged.
                        </p>
                        <MutationStatus
                          pending={prepaidMutation.isPending}
                          error={prepaidMutation.error}
                          success={prepaidMutation.isSuccess ? "Saved" : undefined}
                        />
                      </div>
                      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                        <div className="space-y-1">
                          <label className="block text-xs text-muted-foreground" htmlFor="org-margin">
                            Margin % (blank = platform default)
                          </label>
                          <Input
                            id="org-margin"
                            aria-label={`${orgQuery.data.name} margin`}
                            aria-invalid={orgMarginInvalid}
                            inputMode="decimal"
                            placeholder="Use default"
                            value={orgDraft.margin}
                            onChange={(e) => setOrgDraft((prev) => ({ ...prev, margin: e.target.value }))}
                          />
                        </div>
                        <div className="space-y-1">
                          <label className="block text-xs text-muted-foreground" htmlFor="org-fee">
                            Voice fee per minute (blank = platform default)
                          </label>
                          <Input
                            id="org-fee"
                            aria-label={`${orgQuery.data.name} voice fee`}
                            aria-invalid={orgFeeInvalid}
                            inputMode="decimal"
                            placeholder="Use default"
                            value={orgDraft.fee}
                            onChange={(e) => setOrgDraft((prev) => ({ ...prev, fee: e.target.value }))}
                          />
                        </div>
                      </div>
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={
                          orgMarginInvalid || orgFeeInvalid || !orgHasChanges || updateOrgMutation.isPending
                        }
                        onClick={handleOrgSave}
                      >
                        Save {orgQuery.data.name}
                      </Button>
                      <MutationStatus
                        pending={updateOrgMutation.isPending}
                        error={updateOrgMutation.error}
                        success={updateOrgMutation.isSuccess ? "Saved" : undefined}
                      />
                    </div>
                  ) : null}
                </Card>
              </Section>

              <Section
                title="Adjustments"
                description="Credit or debit the workspace above. Money never moves without a reason."
              >
                <Card className="space-y-3">
                  <form className="space-y-3" onSubmit={handleAdjustmentSubmit}>
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                      <div className="space-y-1">
                        <label className="block text-xs text-muted-foreground" htmlFor="adjust-kind">
                          Kind
                        </label>
                        <Select
                          id="adjust-kind"
                          aria-label="Kind"
                          value={adjustKind}
                          onChange={(e) => setAdjustKind(e.target.value as "adjustment" | "refund")}
                        >
                          <option value="adjustment">Adjustment</option>
                          <option value="refund">Refund</option>
                        </Select>
                      </div>
                      <div className="space-y-1">
                        <label className="block text-xs text-muted-foreground" htmlFor="adjust-amount">
                          Amount in dollars
                        </label>
                        <Input
                          id="adjust-amount"
                          aria-label="Amount in dollars"
                          inputMode="decimal"
                          value={adjustAmount}
                          onChange={(e) => setAdjustAmount(e.target.value)}
                        />
                      </div>
                    </div>

                    <div className="space-y-1">
                      <label className="block text-xs text-muted-foreground" htmlFor="adjust-reason">
                        Reason
                      </label>
                      <Input
                        id="adjust-reason"
                        aria-label="Reason"
                        value={adjustReason}
                        onChange={(e) => setAdjustReason(e.target.value)}
                      />
                    </div>

                    <Button
                      type="submit"
                      disabled={
                        adjustOrgId.trim() === "" ||
                        adjustAmount.trim() === "" ||
                        !Number.isFinite(Number(adjustAmount)) ||
                        adjustReason.trim() === "" ||
                        adjustmentMutation.isPending
                      }
                    >
                      Apply
                    </Button>
                  </form>
                  <MutationStatus
                    pending={adjustmentMutation.isPending}
                    error={adjustmentMutation.error}
                    success={adjustmentMutation.isSuccess ? "Applied" : undefined}
                  />
                </Card>
              </Section>

              <Section
                title="Margin report"
                description={`Last 30 days (${marginRange.from} to ${marginRange.to}).`}
              >
                {marginQuery.isLoading ? (
                  <Spinner label="Loading margin report" />
                ) : marginQuery.isError ? (
                  <div role="alert" className="space-y-2 text-sm text-destructive">
                    <p>{getErrorMessage(marginQuery.error)}</p>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => void marginQuery.refetch()}
                    >
                      Retry
                    </Button>
                  </div>
                ) : marginItems.length === 0 ? (
                  <EmptyState title="No margin data yet." description="Margin data will appear here once usage is recorded." />
                ) : (
                  <Card className="overflow-x-auto p-0">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="border-b border-border text-left text-xs text-muted-foreground">
                          <th className="px-3 py-2 font-medium">Day</th>
                          <th className="px-3 py-2 font-medium">Price</th>
                          <th className="px-3 py-2 font-medium">Cost</th>
                          <th className="px-3 py-2 font-medium">Margin</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {marginItems.map((day) => (
                          <tr key={day.day}>
                            <td className="px-3 py-2">{day.day}</td>
                            <td className="px-3 py-2">{formatCredits(day.price_micros)}</td>
                            <td className="px-3 py-2">{formatCredits(day.cost_micros)}</td>
                            <td className="px-3 py-2">{formatCredits(day.margin_micros)}</td>
                          </tr>
                        ))}
                      </tbody>
                      <tfoot>
                        <tr className="border-t border-border text-left text-xs font-medium">
                          <td className="px-3 py-2">Total</td>
                          <td className="px-3 py-2">{formatCredits(priceTotal)}</td>
                          <td className="px-3 py-2">{formatCredits(costTotal)}</td>
                          <td className="px-3 py-2">{formatCredits(marginTotal)}</td>
                        </tr>
                      </tfoot>
                    </table>
                  </Card>
                )}
              </Section>
            </div>
          )}
        </div>
      ) : (
        <form className="space-y-3" onSubmit={handleUnlock}>
          <p className="text-sm text-muted-foreground">For platform operators.</p>
          <div className="flex items-end gap-2">
            <div className="flex-1 space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor="platform-ops-token">
                Platform operations token
              </label>
              <Input
                id="platform-ops-token"
                aria-label="Platform operations token"
                type="password"
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
              />
            </div>
            <Button type="submit" disabled={inputValue.trim() === ""}>
              Unlock
            </Button>
          </div>
        </form>
      )}
    </Collapsible>
  );
}
