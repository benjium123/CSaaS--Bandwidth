import { useGate, type OrgCapabilities } from "@/api/capabilities";
import {
  OnboardingChecklist,
  isWorkspaceFullySetUp,
} from "@/components/onboarding/OnboardingChecklist";
import { SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";
import { EmptyState, Spinner } from "@/components/ui/primitives";

/**
 * Setup, as a page.
 *
 * It used to be a card wedged above the conversation columns on /inbox, where it and the
 * shell banners together took roughly half the viewport from the thing the operator had
 * actually clicked. The checklist itself is unchanged and still owns its own visibility
 * rule (it reads `useGate()` and renders nothing once the workspace is set up) - this page
 * only gives it room and a destination of its own.
 *
 * The "all done" state is this page's job rather than the checklist's, because a page that
 * renders nothing is a broken page, while a checklist that renders nothing inside a host
 * that has something else to say is correct.
 *
 * PRESENTATION: this is the first real page a new workspace sees, so it gets the
 * reference's large surface (18px, `--cx-surface` over a `--cx-line` hairline), the
 * accent's 145deg gradient on a disc, and pill everything. Every colour is a `--cx-*`
 * token; docs/design/console-reference.html has no square corner and neither does this.
 */

/**
 * How far along the four steps are, as a fraction, for the progress rail in the header.
 *
 * THESE ARE THE SAME FOUR CONDITIONS `isWorkspaceFullySetUp` ANDs together, and they are
 * restated here rather than imported because the checklist exports the verdict, not the
 * parts. If a fifth step is ever added to OnboardingChecklist's `steps`, THIS COUNT MUST
 * MOVE WITH IT or the rail will read "4 of 4" above a list with an unticked row on it.
 * It is display only - nothing branches on it.
 */
function stepsDone(org: OrgCapabilities): { done: number; total: number } {
  const flags = [
    org.has_provider,
    org.has_number,
    org.member_count > 1,
    org.registration_state !== "none",
  ];
  return { done: flags.filter(Boolean).length, total: flags.length };
}

export function SetupPage() {
  const gate = useGate();
  const done = gate.org != null && isWorkspaceFullySetUp(gate.org);
  const progress = gate.org ? stepsDone(gate.org) : null;

  return (
    <div className="mx-auto w-full max-w-3xl space-y-5 p-6 sm:p-8">
      <SurfaceCard className="p-6 sm:p-7">
        <div className="flex items-start gap-4">
          {/* The reference's `.brand-mark`: a disc on the accent ramp, never a square. */}
          <span
            aria-hidden="true"
            className="grid h-12 w-12 flex-none place-items-center rounded-full bg-[linear-gradient(145deg,hsl(var(--cx-accent)),hsl(var(--cx-accent-2)))] text-[17px] font-bold text-[hsl(var(--cx-on-acc))] shadow-[0_10px_24px_-14px_hsl(var(--cx-accent))]"
          >
            ✓
          </span>
          <div className="min-w-0 flex-1">
            <SectionLabel>Getting started</SectionLabel>
            <h1 className="mt-1 text-[24px] font-semibold tracking-[-0.02em] text-[hsl(var(--cx-text))]">
              Setup
            </h1>
            <p className="mt-2 text-[13.5px] leading-[1.55] text-[hsl(var(--cx-subtle))]">
              Everything this workspace still needs before it can call and text.
            </p>
          </div>
          {progress ? (
            <span className="flex-none rounded-full bg-[hsl(var(--cx-overlay))] px-[14px] py-[6px] text-[12.5px] font-semibold text-[hsl(var(--cx-subtle))]">
              {progress.done} of {progress.total} done
            </span>
          ) : null}
        </div>

        {progress ? (
          <div
            aria-hidden="true"
            className="mt-6 flex gap-2"
          >
            {Array.from({ length: progress.total }, (_, i) => (
              <span
                key={i}
                className={
                  i < progress.done
                    ? "h-1.5 flex-1 rounded-full bg-[hsl(var(--cx-accent))]"
                    : "h-1.5 flex-1 rounded-full bg-[hsl(var(--cx-lift))]"
                }
              />
            ))}
          </div>
        ) : null}
      </SurfaceCard>

      {gate.isLoading ? (
        <SurfaceCard className="flex items-center justify-center py-10">
          <Spinner label="Loading setup" />
        </SurfaceCard>
      ) : gate.org == null ? (
        // Capabilities failed. The checklist would render nothing here, and a page with a
        // heading and a void under it reads as "nothing to do" - which is a claim we
        // cannot make out of a failed request.
        <EmptyState
          title="We could not check your setup"
          description="The workspace summary did not load. Reload the page, or open Settings to check providers, numbers and your team directly."
        />
      ) : done ? (
        <EmptyState
          title="You are all set"
          description="A provider is connected, you have a number, your team is invited and registration is under way. Nothing here needs you."
        />
      ) : (
        <OnboardingChecklist />
      )}
    </div>
  );
}
