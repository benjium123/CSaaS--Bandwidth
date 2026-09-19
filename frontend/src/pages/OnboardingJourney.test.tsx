import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { OnboardingPage } from "@/pages/OnboardingPage";
import { SignUpPage } from "@/pages/SignUpPage";
import type { KycPerson, KycProfile, KycStatus } from "@/api/kyc";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { Shell } from "@/App";
import { Sidebar } from "@/components/shell/Sidebar";
import { __resetSurfaceThemeForTests } from "@/auth/useSurfaceTheme";

/**
 * The verification journey.
 *
 * Every test here is written so that it CAN fail. Where a screen's job is to hide something,
 * the test asserting the absence is paired with one asserting the presence on the same
 * component - an `expect(queryBy...).toBeNull()` on its own passes just as happily when the
 * component throws, renders nothing, or was renamed out from under it.
 */

const ME = {
  id: "u1",
  email: "ops@acme.co",
  full_name: "Ops",
  second_factor_required: false,
  memberships: [
    {
      org_id: "org-1",
      org_name: "Acme",
      org_slug: "acme",
      role_name: "admin",
      permissions: ["org:read", "org:update"],
    },
  ],
  permissions: ["org:read", "org:update"],
};

function person(over: Partial<KycPerson> = {}): KycPerson {
  return {
    id: "p1",
    role: "owner",
    full_name: "Jane Smith",
    email: null,
    ownership_percent: 100,
    is_user: false,
    is_you: false,
    status: "not_started",
    verified_name: null,
    document_country: null,
    verified_at: null,
    last_error: null,
    residential_address: null,
    ...over,
  };
}

function profile(status: KycStatus, over: Partial<KycProfile> = {}): KycProfile {
  return {
    status,
    business: {
      country: null, legal_name: null, dba_name: null, entity_type: null,
      registration_number: null, tax_id: null, incorporation_date: null,
      registered_address: null, operating_address: null, website: null,
      business_email: null, business_phone: null,
    },
    use_case: null,
    use_case_pending: null,
    persons: [],
    documents: [],
    checks: {},
    agreement: { current_version: "1", accepted_version: null, accepted_at: null },
    missing: [],
    info_request: null,
    submitted_at: null,
    decided_at: null,
    decision_reason: null,
    limits: null,
    deposit_required_cents: null,
    next_reverification_at: null,
    ...over,
  };
}

function render(p: KycProfile) {
  return renderWithProviders(
    <OnboardingPage />,
    makeStubClient({ "/api/v1/auth/me": ME, "/api/v1/kyc/profile": p }),
  );
}

describe("OnboardingPage — the stepper is mounted only where `missing` means something", () => {
  // The PAIR. `missing` is [] in every status except draft/needs_info, so a stepper rendered
  // anywhere else would tick all six steps green off an empty array. The first test proves
  // the stepper can appear at all; the second proves it does not appear where [] is a fact
  // about the payload rather than an achievement. Neither is meaningful without the other.
  it("renders the steps in draft", async () => {
    render(profile("draft", { missing: ["legal_name", "owner", "agreement"] }));
    expect(await screen.findByText("Your business")).toBeTruthy();
    expect(screen.getByText("Agreement")).toBeTruthy();
  });

  it("renders no steps when rejected, though `missing` is empty there too", async () => {
    render(profile("rejected", { decision_reason: "Unregistered entity." }));
    // Anchored on the screen having actually rendered, so the absence below is an absence
    // in a real tree rather than in a tree that never mounted.
    expect(await screen.findByText("We couldn't verify this business")).toBeTruthy();
    expect(screen.queryByText("Your business")).toBeNull();
    expect(screen.queryByText("Agreement")).toBeNull();
  });
});

describe("OnboardingPage — counts come from the server's list", () => {
  it("counts only the keys a step owns, and marks the rest done", async () => {
    // An owner EXISTS here, and that is load-bearing rather than decoration: without one the
    // per-owner steps are unassessable and three of the four "Done"s below would be the very
    // false tick this file now tests for. See the "positive evidence" block.
    render(
      profile("draft", {
        missing: ["legal_name", "website", "agreement"],
        persons: [person()],
      }),
    );
    // Two business keys, one agreement key, nothing for the other four steps.
    expect(await screen.findByText("2 left")).toBeTruthy();
    expect(screen.getByText("1 left")).toBeTruthy();
    expect(screen.getAllByText("Done")).toHaveLength(4);
  });

  it("offers submission only when the server says nothing is missing", async () => {
    render(profile("draft", { missing: [] }));
    expect(await screen.findByRole("button", { name: "Submit for review" })).toBeTruthy();
  });

  it("withholds submission while anything is missing", async () => {
    render(profile("draft", { missing: ["agreement"] }));
    expect(await screen.findByText("1 left")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Submit for review" })).toBeNull();
  });
});

/**
 * The exact `missing` a brand-new profile comes back with, copied from a live response. The
 * point of pasting all eighteen keys rather than a representative three is what is NOT in
 * it: no `id_verification`, no `proof_of_address`, no `residential_address`. The server
 * cannot compute those before an owner row exists, and the page used to read that silence as
 * a completed identity check.
 */
const FRESH_PROFILE_MISSING = [
  "country", "legal_name", "entity_type", "registration_number", "registered_address",
  "website", "business_email", "business_phone", "use_case.description", "use_case.vertical",
  "use_case.who_you_contact", "use_case.list_source", "use_case.monthly_calls",
  "use_case.monthly_texts", "use_case.destination_countries", "owner", "documents",
  "agreement",
];

function step(title: string): HTMLElement {
  const li = screen.getByText(title).closest("li");
  if (!li) throw new Error("no step row for " + title);
  return li as HTMLElement;
}
const tagOf = (title: string) => step(title).querySelector(".ob-step-tag")?.textContent?.trim();
const nodeOf = (title: string) => step(title).querySelector(".ob-node")?.textContent?.trim();

describe("OnboardingPage — a tick is positive evidence, never an absence", () => {
  it("does not call the ID check done on a profile that has no owner to check", async () => {
    render(profile("draft", { missing: FRESH_PROFILE_MISSING, persons: [] }));
    await screen.findByText("Prove it's you");

    // Three ways of saying it, because the bug wore all three: the word, the class and the
    // tick. Any one of them alone would survive the others being fixed.
    expect(tagOf("Prove it's you")).not.toBe("Done");
    expect(step("Prove it's you").className).not.toContain("is-done");
    expect(nodeOf("Prove it's you")).not.toBe("✓");

    expect(tagOf("Prove it's you")).toBe("Add an owner first");
    expect(step("Prove it's you").className).toContain("is-waiting");
  });

  it("calls it todo, with a count, once an owner exists and the key is outstanding", async () => {
    render(
      profile("draft", {
        missing: ["id_verification"],
        persons: [person()],
      }),
    );
    await screen.findByText("Prove it's you");
    expect(tagOf("Prove it's you")).toBe("1 left");
    expect(step("Prove it's you").className).not.toContain("is-done");
  });

  // The pair that stops the first test passing vacuously: same absent key, an owner present,
  // and now the absence IS evidence. Without this, "never done" would pass on a page that had
  // simply stopped rendering "Done" at all.
  it("calls it done once an owner exists and the key is absent from missing", async () => {
    render(profile("draft", { missing: ["agreement"], persons: [person()] }));
    await screen.findByText("Prove it's you");
    expect(tagOf("Prove it's you")).toBe("Done");
    expect(step("Prove it's you").className).toContain("is-done");
    expect(nodeOf("Prove it's you")).toBe("✓");
  });

  it("never calls Documents done while there is no owner, and shows no half-count", async () => {
    render(profile("draft", { missing: FRESH_PROFILE_MISSING, persons: [] }));
    await screen.findByText("Documents");
    // `documents` IS outstanding, but `proof_of_address` cannot be assessed - so "1 left"
    // would announce a step that is one upload from finished when nobody has looked at half
    // of it.
    expect(tagOf("Documents")).toBe("Add an owner first");
    expect(tagOf("Documents")).not.toBe("1 left");
    expect(step("Documents").className).not.toContain("is-done");
  });

  it("does not call Owners done on an absence when no owner person exists", async () => {
    render(profile("draft", { missing: ["agreement"], persons: [] }));
    await screen.findByText("Owners");
    expect(step("Owners").className).not.toContain("is-done");
    expect(tagOf("Owners")).toBe("Add an owner first");
  });

  it("still treats absence as evidence for the steps that always apply", async () => {
    // business, use_case and agreement are computed for every draft profile, so an empty
    // filter there really is a completion - and must stay one, or the fix would have turned
    // the whole stepper into a page that can never say Done.
    render(profile("draft", { missing: ["owner"], persons: [] }));
    await screen.findByText("Your business");
    expect(tagOf("Your business")).toBe("Done");
    expect(tagOf("How you'll use it")).toBe("Done");
    expect(tagOf("Agreement")).toBe("Done");
    expect(tagOf("Owners")).toBe("1 left");
  });
});

describe("OnboardingPage — the reviewer's words are not paraphrased", () => {
  it("renders info_request verbatim in needs_info", async () => {
    const note = "Your utility bill is older than 90 days. Send one from the last quarter.";
    render(profile("needs_info", { missing: ["proof_of_address"], info_request: note }));
    // textContent equality, not toHaveTextContent: that does substring matching and would
    // pass with our own copy wrapped around the reviewer's.
    const el = await screen.findByText(note);
    expect(el.textContent?.trim()).toBe(note);
  });

  it("renders decision_reason verbatim when rejected, with no way to retry", async () => {
    const why = "The registration number does not match any active entity.";
    render(profile("rejected", { decision_reason: why }));
    const el = await screen.findByText(why);
    expect(el.textContent?.trim()).toContain(why);
    // No transition leaves `rejected`, so any of these would be a control that resolves to
    // nothing. Paired with the assertion above, which proves the screen rendered.
    expect(screen.queryByRole("button", { name: /appeal|retry|try again|resubmit/i })).toBeNull();
  });
});

describe("OnboardingPage — reverification offers a button only where the call would work", () => {
  // The stale test is the whole point. A profile moves to `reverification_due` WITHOUT
  // touching person rows, so every owner is still `verified`; a filter on status alone
  // returns nobody and the screen silently offers nothing. These three cases differ only in
  // who the owner is, and each expects a different control.
  const LAST_YEAR = "2025-09-01T00:00:00+00:00";
  const DUE = "2026-09-01T00:00:00+00:00";

  it("offers me my own re-check when the stale owner is me", async () => {
    render(
      profile("reverification_due", {
        next_reverification_at: DUE,
        persons: [person({ is_user: true, is_you: true, status: "verified", verified_at: LAST_YEAR })],
      }),
    );
    expect(await screen.findByRole("button", { name: "Re-check my ID" })).toBeTruthy();
  });

  it("names another member's owner without a button, because only they can start it", async () => {
    render(
      profile("reverification_due", {
        next_reverification_at: DUE,
        persons: [person({ is_user: true, is_you: false, status: "verified", verified_at: LAST_YEAR })],
      }),
    );
    expect(await screen.findByText(/Waiting on Jane Smith/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Re-check my ID" })).toBeNull();
  });

  it("offers a link for an owner with no account, which anyone with org:update may raise", async () => {
    render(
      profile("reverification_due", {
        next_reverification_at: DUE,
        persons: [person({ is_user: false, is_you: false, status: "verified", verified_at: LAST_YEAR })],
      }),
    );
    expect(await screen.findByRole("button", { name: "Get their link" })).toBeTruthy();
    expect(screen.queryByText(/Waiting on Jane Smith/)).toBeNull();
  });

  it("treats an owner who re-checked AFTER the cutoff as done", async () => {
    render(
      profile("reverification_due", {
        next_reverification_at: DUE,
        persons: [
          person({ is_you: true, status: "verified", verified_at: "2026-09-02T00:00:00+00:00" }),
        ],
      }),
    );
    expect(await screen.findByText(/confirming it now/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Re-check my ID" })).toBeNull();
  });
});

describe("OnboardingPage — the waiting and approved screens claim only what they know", () => {
  it("shows no estimate while a reviewer has it", async () => {
    render(profile("in_review", { submitted_at: "2026-09-10T00:00:00+00:00" }));
    expect(await screen.findByText("With a reviewer")).toBeTruthy();
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("states the starting limits rather than an unqualified all-clear", async () => {
    render(
      profile("approved", {
        decided_at: "2026-09-12T00:00:00+00:00",
        limits: { daily_texts: 500 },
      }),
    );
    expect(await screen.findByText("What you start with")).toBeTruthy();
    expect(screen.getByText("daily texts")).toBeTruthy();
    expect(screen.getByText("500")).toBeTruthy();
  });

  it("says a use-case change is still pending rather than letting it read as saved", async () => {
    render(
      profile("approved", {
        use_case_pending: {
          description: "Appointment reminders", vertical: "healthcare",
          who_you_contact: "patients", list_source: "bookings",
          monthly_calls: 100, monthly_texts: 900, destination_countries: ["US"],
        },
      }),
    );
    expect(await screen.findByText(/is with a\s+reviewer/)).toBeTruthy();
  });

  it("gives no suspension reason, because the payload carries none", async () => {
    render(profile("suspended"));
    expect(await screen.findByText("This account is suspended")).toBeTruthy();
    expect(screen.getByText(/emailed the account owners/)).toBeTruthy();
  });
});

describe("SignUpPage — the consumer-domain hint is a courtesy, not a gate", () => {
  it("warns on a personal address but leaves the form submittable", async () => {
    renderWithProviders(<SignUpPage />, makeStubClient({ "/api/v1/auth/me": ME }));
    await userEvent.type(screen.getByLabelText("Work email"), "someone@gmail.com");
    await userEvent.type(screen.getByLabelText("Your name"), "Someone");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery staple");
    expect(screen.getByText(/looks like a personal address/)).toBeTruthy();
    // The server is the authority. If our list is ever wrong about a legitimate domain, a
    // locked button would be an unappealable client-side refusal.
    await waitFor(() => {
      const btn = screen.getByRole("button", { name: "Create account" }) as HTMLButtonElement;
      expect(btn.disabled).toBe(false);
    });
  });

  it("does not warn on a company address", async () => {
    renderWithProviders(<SignUpPage />, makeStubClient({ "/api/v1/auth/me": ME }));
    await userEvent.type(screen.getByLabelText("Work email"), "someone@acme.co");
    expect(screen.queryByText(/looks like a personal address/)).toBeNull();
    // Paired with the assertion above: proves the form is mounted and the hint simply is
    // not showing, rather than the whole screen having failed to render.
    expect(screen.getByRole("button", { name: "Create account" })).toBeTruthy();
  });
});

describe("AuthAside — the equipment spec rows are gone", () => {
  it("keeps the aside but carries none of the removed specification copy", () => {
    // The pair: the aside must still RENDER (first assertion), or the four absences below
    // would pass on a page that failed to mount and prove nothing at all.
    renderWithProviders(<SignUpPage />, makeStubClient({ "/api/v1/auth/me": ME }));
    expect(screen.getByText(/Communications software for teams that answer the phone/)).toBeTruthy();

    for (const gone of [
      "Passkeys · authenticator app · recovery codes",
      "SAML 2.0 · OIDC · SCIM user sync",
      "HttpOnly cookie, idle and absolute limits",
      "United States · United Kingdom",
    ]) {
      expect(screen.queryByText(gone)).toBeNull();
    }
  });
});

/**
 * The console's theme, and the one preference behind it.
 *
 * WHAT THESE TESTS CANNOT DO. Vitest runs with `css: false`, so no stylesheet is ever
 * parsed here and nothing below is evidence that the light console is READABLE. Contrast
 * was measured by computing the WCAG ratio for every pair the light theme introduces; that
 * work lives in the comment header of consoleTheme.light.css and cannot be asserted from
 * jsdom. What these tests do pin is the wiring: which class the console emits, and that the
 * control inside the console writes the same storage key the front door reads. Those are
 * the two things that broke - the console was hardcoded `dark`, so signing in flipped the
 * product under someone who had just chosen light.
 *
 * Each theme assertion is written as a PAIR - light must emit `is-light` AND NOT `dark`,
 * dark must emit `dark` AND NOT `is-light`. A lone `not.toContain("dark")` would pass just
 * as happily on a Shell that rendered no className at all, or that failed to mount.
 *
 * BOTH cases must also emit `console-surface`, and that is a THIRD thing, not a restatement
 * of the theme. `console-surface` is the scope selector consoleTheme.css hangs its token
 * block off - the one that re-points --background, --foreground, --border, --primary, --muted
 * and --muted-foreground at the approved palette - while `is-light`/`dark` only choose WHICH
 * palette. The class used to sit on ConversationsPage alone, so every other console page fell
 * outside the scope and rendered the generic shadcn tokens: the reported "no page has had its
 * theme changed". It is theme-independent by construction, so it is asserted in both cases;
 * a regression that dropped it would otherwise still pass the light/dark pair.
 */
/** The Shell and the Sidebar both mount the capability gate, which needs a real shape. */
function themeStubClient() {
  return makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/me/capabilities": {
      permissions: ["org:read", "org:update"],
      org: { has_provider: false, has_number: false, member_count: 1, registration_state: "none" },
    },
  });
}

describe("Shell — the console follows the one stored theme preference", () => {
  const KEY = "csaas.surface-theme";

  beforeEach(() => {
    window.localStorage.clear();
    __resetSurfaceThemeForTests();
  });

  afterEach(() => {
    window.localStorage.clear();
    __resetSurfaceThemeForTests();
  });

  /** The Shell's own wrapper: the first element carrying one of the two theme classes. */
  function shellWrapper(container: HTMLElement): HTMLElement {
    const found = container.querySelector<HTMLElement>(".dark, .is-light");
    if (!found) throw new Error("the Shell rendered neither theme class");
    return found;
  }

  it("emits is-light and never dark when the stored preference is light", () => {
    window.localStorage.setItem(KEY, "light");
    const { container } = renderWithProviders(
      <Shell>
        <div>console body</div>
      </Shell>,
      themeStubClient(),
    );

    expect(screen.getByText("console body")).toBeTruthy();
    const classes = shellWrapper(container).className.split(/\s+/);
    expect(classes).toContain("is-light");
    expect(classes).not.toContain("dark");
    expect(classes).toContain("console-surface");
  });

  it("emits dark and never is-light when the stored preference is dark", () => {
    window.localStorage.setItem(KEY, "dark");
    const { container } = renderWithProviders(
      <Shell>
        <div>console body</div>
      </Shell>,
      themeStubClient(),
    );

    expect(screen.getByText("console body")).toBeTruthy();
    const classes = shellWrapper(container).className.split(/\s+/);
    expect(classes).toContain("dark");
    expect(classes).not.toContain("is-light");
    expect(classes).toContain("console-surface");
  });

  it("defaults to the same light the front door defaults to when nothing is stored", () => {
    // The default lives in useSurfaceTheme and is deliberately light, so a first-time
    // visitor meets the same product on both sides of the sign-in form. If that default is
    // ever reversed this test is the one that should be changed, not worked around.
    const { container } = renderWithProviders(
      <Shell>
        <div>console body</div>
      </Shell>,
      themeStubClient(),
    );

    expect(window.localStorage.getItem(KEY)).toBeNull();
    const classes = shellWrapper(container).className.split(/\s+/);
    expect(classes).toContain("is-light");
    expect(classes).not.toContain("dark");
    expect(classes).toContain("console-surface");
  });
});

describe("Sidebar — the console's theme control is the front door's control", () => {
  const KEY = "csaas.surface-theme";

  beforeEach(() => {
    window.localStorage.clear();
    __resetSurfaceThemeForTests();
  });

  afterEach(() => {
    window.localStorage.clear();
    __resetSurfaceThemeForTests();
  });

  it("writes the key the public surface reads, under the accessible name of the ACTION", async () => {
    window.localStorage.setItem(KEY, "dark");
    renderWithProviders(<Sidebar />, themeStubClient());

    // On a dark console the control offers LIGHT - the name states what pressing it does,
    // not where you are. Finding it by that name is also what proves ThemeToggle itself was
    // reused rather than a second control invented in the rail.
    const button = await screen.findByRole("button", { name: "Switch to the light theme" });
    await userEvent.click(button);

    expect(window.localStorage.getItem(KEY)).toBe("light");
    // And back, so this cannot pass on a control that only ever writes "light".
    await userEvent.click(
      await screen.findByRole("button", { name: "Switch to the dark theme" }),
    );
    expect(window.localStorage.getItem(KEY)).toBe("dark");
  });

  it("moves the rail's own wrapper, not only the stored value", async () => {
    window.localStorage.setItem(KEY, "dark");
    const { container } = renderWithProviders(
      <Sidebar />,
      themeStubClient(),
    );

    const aside = container.querySelector("aside");
    expect(aside).toBeTruthy();
    expect(aside!.className.split(/\s+/)).toContain("dark");

    await userEvent.click(
      await screen.findByRole("button", { name: "Switch to the light theme" }),
    );

    expect(aside!.className.split(/\s+/)).toContain("is-light");
    expect(aside!.className.split(/\s+/)).not.toContain("dark");
  });
});
