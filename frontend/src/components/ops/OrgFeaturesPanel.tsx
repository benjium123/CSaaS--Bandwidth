import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import { Spinner, mutationErrorMessage } from "@/components/ui/primitives";
import { ConsoleEmpty, SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";

/** Per-workspace feature switches. The server owns the authoritative on/off state and bills
 * along the switch (a feature that is off is never used, so it is never charged), so we never
 * fake a state change locally: every toggle is a PUT and the list is refetched. Writes need an
 * admin operator; a reviewer gets a 403 that we surface verbatim rather than pretending the
 * change stuck. */

export type OrgFeature = {
  key: string;
  label: string;
  group: string;
  description: string;
  default_enabled: boolean;
  enabled: boolean;
  price_metric: string | null;
};

type ConsoleApi = ReturnType<typeof useAuth>["api"];

type FeatureToggle = { key: string; enabled: boolean };

type OrgFeatureToggleResult = { org_id: string; key: string; enabled: boolean };

/** Root of the query keys used by the console hooks in @/api/opsConsole (useConsoleOrgs et al).
 * Invalidating it refetches this list *and* the org rows rendered by ConsoleTab, since a
 * feature that stops billing changes what a workspace is charged. */
const CONSOLE_QUERY_ROOT = ["ops", "console"] as const;

export function orgFeaturesQueryKey(orgId: string) {
  return [...CONSOLE_QUERY_ROOT, "org-features", orgId] as const;
}

/** The order the operator reads the switches in; a group the backend adds that we do not know
 * yet is appended after these, in payload order, rather than being dropped. */
const GROUP_ORDER = ["Calling", "Messaging", "AI", "Numbers", "Platform"];

function featuresPath(orgId: string): string {
  return `/api/v1/ops/console/orgs/${orgId}/features`;
}

function featurePath(orgId: string, key: string): string {
  return `${featuresPath(orgId)}/${key}`;
}

/** The "this call may be recorded" notice: on by default, only super admins switch it. It
 * plays only on calls the workspace records. */
export type RecordingNotice = { enabled: boolean; record_calls: boolean };

type OrgFeaturesPayload = { features: OrgFeature[]; recordingNotice: RecordingNotice | null };

async function fetchOrgFeatures(api: ConsoleApi, orgId: string): Promise<OrgFeaturesPayload> {
  const data = await api.request<{
    org_id: string;
    features: OrgFeature[];
    recording_notice?: RecordingNotice;
  }>(featuresPath(orgId));
  return { features: data.features ?? [], recordingNotice: data.recording_notice ?? null };
}

async function setRecordingNotice(api: ConsoleApi, orgId: string, enabled: boolean) {
  return api.request(`/api/v1/ops/console/orgs/${orgId}/recording-notice`, {
    method: "PUT",
    json: { enabled },
  });
}

async function setOrgFeature(
  api: ConsoleApi,
  orgId: string,
  key: string,
  enabled: boolean,
): Promise<OrgFeatureToggleResult> {
  return api.request<OrgFeatureToggleResult>(featurePath(orgId, key), {
    method: "PUT",
    json: { enabled },
  });
}

function groupFeatures(features: OrgFeature[]): { group: string; features: OrgFeature[] }[] {
  const byGroup = new Map<string, OrgFeature[]>();
  for (const feature of features) {
    const bucket = byGroup.get(feature.group);
    if (bucket) bucket.push(feature);
    else byGroup.set(feature.group, [feature]);
  }

  const ordered: { group: string; features: OrgFeature[] }[] = [];
  for (const group of GROUP_ORDER) {
    const bucket = byGroup.get(group);
    if (bucket && bucket.length > 0) {
      ordered.push({ group, features: bucket });
      byGroup.delete(group);
    }
  }
  for (const [group, bucket] of byGroup) ordered.push({ group, features: bucket });
  return ordered;
}

export function OrgFeaturesPanel({
  orgId,
  canEdit,
}: {
  orgId: string;
  canEdit: boolean;
}): JSX.Element {
  const { api } = useAuth();
  const queryClient = useQueryClient();

  const featuresQuery = useQuery({
    queryKey: orgFeaturesQueryKey(orgId),
    queryFn: () => fetchOrgFeatures(api, orgId),
  });

  const toggleMutation = useMutation({
    mutationFn: ({ key, enabled }: FeatureToggle) => setOrgFeature(api, orgId, key, enabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: orgFeaturesQueryKey(orgId) });
      void queryClient.invalidateQueries({ queryKey: CONSOLE_QUERY_ROOT });
    },
  });

  const noticeMutation = useMutation({
    mutationFn: (enabled: boolean) => setRecordingNotice(api, orgId, enabled),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: orgFeaturesQueryKey(orgId) });
    },
  });

  const features = React.useMemo(() => featuresQuery.data?.features ?? [], [featuresQuery.data]);
  const notice = featuresQuery.data?.recordingNotice ?? null;
  const grouped = React.useMemo(() => groupFeatures(features), [features]);
  const pendingKey = toggleMutation.isPending
    ? toggleMutation.variables?.key ?? null
    : null;

  return (
    <SurfaceCard className="space-y-3">
      <SectionLabel>Features</SectionLabel>
      <p className="text-xs text-muted-foreground">
        Switch features on or off for this workspace. Charges follow the switch: a feature that is
        off is never used or billed.
      </p>

      {!canEdit ? (
        <p className="text-xs text-muted-foreground">
          Read-only: only operator admins can change features.
        </p>
      ) : null}

      {featuresQuery.isPending ? (
        <Spinner label="Loading features" />
      ) : featuresQuery.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(featuresQuery.error)}
        </p>
      ) : features.length > 0 ? (
        <div className="space-y-4">
          {notice ? (
            <div className="flex items-start justify-between gap-4 rounded-[var(--cx-r-md,14px)] border border-border px-3 py-2">
              <div className="min-w-0 space-y-1">
                <span className="text-[13px] font-medium">Recording notice</span>
                <p className="text-xs text-muted-foreground">
                  Plays &quot;this call may be recorded&quot; when the other side answers, on calls
                  this workspace records. On by default; only super admins can change it.
                  {notice.record_calls ? "" : " Call recording is off, so it does not play now."}
                </p>
              </div>
              <label className="relative inline-flex shrink-0 cursor-pointer items-center">
                <input
                  type="checkbox"
                  role="switch"
                  aria-label="Recording notice"
                  className="peer sr-only"
                  checked={notice.enabled}
                  disabled={!canEdit || noticeMutation.isPending}
                  onChange={(event) => noticeMutation.mutate(event.target.checked)}
                />
                <span
                  aria-hidden="true"
                  className="h-5 w-9 rounded-full bg-muted transition-colors peer-checked:bg-muted-foreground peer-disabled:opacity-50"
                />
                <span
                  aria-hidden="true"
                  className="absolute left-0.5 top-0.5 h-4 w-4 rounded-full border border-border bg-background transition-transform peer-checked:translate-x-4"
                />
              </label>
            </div>
          ) : null}
          {grouped.map((group) => (
            <div key={group.group} className="space-y-2">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {group.group}
              </h4>
              <ul className="rounded-[var(--cx-r-md,14px)] border border-border">
                {group.features.map((feature) => {
                  const changed = feature.enabled !== feature.default_enabled;
                  return (
                    <li
                      key={feature.key}
                      className="flex items-start justify-between gap-4 border-t border-border px-3 py-2 first:border-t-0"
                    >
                      <div className="min-w-0 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-[13px] font-medium">{feature.label}</span>
                          <span
                            className={
                              changed
                                ? "rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium"
                                : "rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] text-muted-foreground"
                            }
                          >
                            {changed
                              ? "Changed"
                              : feature.default_enabled
                                ? "Default on"
                                : "Default off"}
                          </span>
                        </div>
                        <p className="text-xs text-muted-foreground">{feature.description}</p>
                        <p className="text-xs text-muted-foreground">
                          {feature.price_metric
                            ? `Billed as ${feature.price_metric}`
                            : "No separate charge"}
                        </p>
                      </div>
                      <label className="relative inline-flex shrink-0 cursor-pointer items-center">
                        <input
                          type="checkbox"
                          role="switch"
                          aria-label={feature.label}
                          className="peer sr-only"
                          checked={feature.enabled}
                          disabled={!canEdit || pendingKey === feature.key}
                          onChange={(event) =>
                            toggleMutation.mutate({
                              key: feature.key,
                              enabled: event.target.checked,
                            })
                          }
                        />
                        <span
                          aria-hidden="true"
                          className="h-5 w-9 rounded-full bg-muted transition-colors peer-checked:bg-muted-foreground peer-disabled:opacity-50"
                        />
                        <span
                          aria-hidden="true"
                          className="absolute left-0.5 top-0.5 h-4 w-4 rounded-full border border-border bg-background transition-transform peer-checked:translate-x-4"
                        />
                      </label>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
      ) : (
        <ConsoleEmpty>No features yet.</ConsoleEmpty>
      )}

      {noticeMutation.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(noticeMutation.error)}
        </p>
      ) : null}

      {toggleMutation.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(toggleMutation.error)}
        </p>
      ) : null}
    </SurfaceCard>
  );
}
