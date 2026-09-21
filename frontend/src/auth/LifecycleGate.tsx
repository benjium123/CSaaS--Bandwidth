/**
 * LifecycleGate - where a signed-in workspace is allowed to be.
 *
 * The backend is the authority on what a workspace may DO; this gate only decides what is
 * RENDERED, exactly as api/capabilities.ts says of itself. It belongs INSIDE the
 * authenticated route table, after the platform-operator branch, so it never sees - and
 * never special-cases - admin/operator routing.
 *
 * Read the rules in this order:
 *  1. FAIL CLOSED while the answer is missing. Capabilities still loading -> a full-page
 *     loading surface, no children. An org we cannot read an onboarding step for, or a step
 *     value this client cannot validate -> a retryable blocked surface, no children.
 *     "We have not asked yet" and "we do not know" are not "carry on".
 *  2. `ready` renders its children on whatever route it is on.
 *  3. Any other step renders children ONLY on the routes that step owns - plus /plans,
 *     which every unfinished workspace may read - and is otherwise redirected, with
 *     `replace`, to that step's landing route.
 *
 * Loops are impossible by construction: every landing route is also in its own step's
 * allow-list, so a redirect settles on the first hop. Deep links are not honoured: a path a
 * step does not own (including another step's settings page) is redirected away, so the URL
 * bar cannot be used to skip setup.
 */
import * as React from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { CAPABILITIES_QUERY_KEY, isOnboardingStep, useGate } from "@/api/capabilities";
import { AuthAlert, AuthButton, AuthPlate, AuthSurface, Lamp } from "@/components/auth/AuthShell";

/** One row per unfinished step: where it lands, and where it may render. */
type SetupRouteRules = {
  landingRoute: string;
  allowedRoutes: readonly string[];
};

/**
 * Object-valued rather than a union-typed map on purpose: the step names come from the
 * server (`org.onboarding_step`) and are validated by isOnboardingStep, so this file holds
 * the ROUTING rules only and no second copy of the step list to drift from the API's.
 *
 * Every landingRoute appears in its own allowedRoutes - that is what makes the redirect
 * settle instead of bounce. /settings/verification is for the steps that still owe us
 * documents (verification and remediation) and not for awaiting_review, which is past that
 * step; /settings/numbers only for the step that owes us a number; /plans, and nothing
 * else, is readable from every unfinished step.
 */
const SETUP_ROUTE_RULES: Readonly<Record<string, SetupRouteRules>> = {
  verification: {
    landingRoute: "/verification",
    allowedRoutes: ["/verification", "/onboarding", "/settings/verification", "/plans"],
  },
  awaiting_review: {
    landingRoute: "/onboarding",
    allowedRoutes: ["/verification", "/onboarding", "/settings/verification", "/plans"],
  },
  remediation: {
    landingRoute: "/verification",
    allowedRoutes: ["/verification", "/onboarding", "/settings/verification", "/plans"],
  },
  numbers: {
    landingRoute: "/settings/numbers",
    allowedRoutes: ["/settings/numbers", "/plans"],
  },
};

/** No rules for a validated step is still "we do not know where to send you": fail closed. */
function routeRulesFor(step: string): SetupRouteRules | null {
  if (!Object.prototype.hasOwnProperty.call(SETUP_ROUTE_RULES, step)) return null;
  const rules: SetupRouteRules | undefined = SETUP_ROUTE_RULES[step];
  return rules ?? null;
}

function stripTrailingSlash(pathname: string): string {
  return pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
}

/** Exact match, or a descendant of that route. `/onboarding-x` must not pass for `/onboarding`. */
function isRouteAllowed(pathname: string, allowed: readonly string[]): boolean {
  const path = stripTrailingSlash(pathname);
  return allowed.some((route) => path === route || path.startsWith(`${route}/`));
}

/** Both failure surfaces are the same plate, so a blocked workspace still looks designed. */
function GateSurface({
  title,
  lede,
  children,
}: {
  title: string;
  lede: string;
  children: React.ReactNode;
}) {
  // aside={null}: this is a state of the product, not the front door, so no marketing column.
  return (
    <AuthSurface aside={null}>
      <AuthPlate eyebrow="Workspace setup" title={title} lede={lede}>
        {children}
      </AuthPlate>
    </AuthSurface>
  );
}

/** The closed door: an alert, and the one action that can change the answer. */
function BlockedSurface({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <GateSurface
      title="We could not check your workspace"
      lede="Nothing is shown until we can confirm where this workspace is up to."
    >
      <div className="flex flex-col gap-4">
        <AuthAlert>{message}</AuthAlert>
        <AuthButton block onClick={onRetry}>
          Try again
        </AuthButton>
      </div>
    </GateSurface>
  );
}

export function LifecycleGate({ children }: { children: React.ReactNode }) {
  const gate = useGate();
  const location = useLocation();
  const queryClient = useQueryClient();

  // The capabilities query runs with retry:false, so a manual retry is the only way back
  // from a failed lookup. Invalidating the key refetches it and re-runs every rule below.
  const retry = React.useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: CAPABILITIES_QUERY_KEY });
  }, [queryClient]);

  // 1. Still deciding. Render the waiting surface and no children - a gate that renders the
  // app while it does not yet know the answer is not a gate.
  if (gate.isLoading) {
    return (
      <GateSurface
        title="Loading your workspace"
        lede="Just a moment while we confirm where this workspace is up to."
      >
        <Lamp state="wait">Checking your workspace setup…</Lamp>
      </GateSurface>
    );
  }

  // 2. Validate the server's step. `ready` is accepted on its own name as well as through
  // the guard: it is the terminal step, and a step list that omitted the finished value
  // must not be able to strand a finished workspace in setup. Everything else - missing,
  // an unrecognised string, an errored capabilities lookup (gate.org is null then) - fails
  // closed on the surface below, which renders no children.
  const step: unknown = gate.org?.onboarding_step;
  if (step === "ready") return <>{children}</>;

  const rules = isOnboardingStep(step) ? routeRulesFor(step) : null;
  if (!rules) {
    return (
      <BlockedSurface
        message="We could not load the setup state for this workspace."
        onRetry={retry}
      />
    );
  }

  // 3. Children only on the routes this step owns; otherwise its landing route. `replace`
  // keeps the blocked URL out of history, so Back cannot re-enter it.
  if ((step === "verification" || step === "remediation") && location.pathname === "/onboarding") {
    return <Navigate to="/verification" replace />;
  }
  if (isRouteAllowed(location.pathname, rules.allowedRoutes)) return <>{children}</>;
  return <Navigate to={rules.landingRoute} replace />;
}
