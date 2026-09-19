import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { InboxColumn, lineInitials } from "./InboxColumn";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import type { Inbox } from "@/api/conversations";
import { openCommandPalette } from "@/components/ui/CommandPalette";

// The rail's magnifier must open the ONE command palette the app already has, not a
// second search of its own. Spying on the real module's export is what proves the wire
// is connected to that module and not to a lookalike.
vi.mock("@/components/ui/CommandPalette", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/ui/CommandPalette")>();
  return { ...actual, openCommandPalette: vi.fn() };
});

const ME = {
  id: "u1",
  email: "u@example.com",
  full_name: "U Ser",
  memberships: [
    { org_id: "org-1", org_name: "Acme Plumbing", org_slug: "acme", role_name: "owner" },
  ],
};

/** Everything the rail can gate on, so the default render shows the full navigation. */
const FULL_CAPS = {
  permissions: [
    "contacts:read",
    "calls:read",
    "campaigns:read",
    "org:read",
    "settings:read",
  ],
  org: {
    has_provider: true,
    has_number: true,
    member_count: 2,
    registration_state: "approved",
  },
};

/** The backend agent role: no campaigns:read, and no settings section it can view. */
const AGENT_CAPS = {
  permissions: ["inbox:read", "inbox:send", "contacts:read", "calls:read"],
  org: FULL_CAPS.org,
};

function inbox(overrides: Partial<Inbox> = {}): Inbox {
  return {
    id: "i1",
    name: "Sales",
    color: "#22c55e",
    e164: "+14694617576",
    number_id: "n1",
    my_role: "admin",
    ...overrides,
  };
}

const inboxes = [
  inbox(),
  inbox({
    id: "i2",
    name: "Support",
    color: "#3b82f6",
    e164: "+12145550111",
    number_id: "n2",
  }),
];

function renderColumn(
  overrides: Partial<React.ComponentProps<typeof InboxColumn>> = {},
  capabilities: unknown = FULL_CAPS,
  me: unknown = ME,
) {
  const props: React.ComponentProps<typeof InboxColumn> = {
    inboxes,
    isLoading: false,
    error: null,
    selection: { kind: "all" },
    onSelect: vi.fn(),
    unread: {},
    unreadTruncated: false,
    ...overrides,
  };
  const client = makeStubClient({
    "/api/v1/auth/me": me,
    "/api/v1/me/capabilities": capabilities,
    "/api/v1/notifications": { items: [], unread_count: 0 },
  });
  return { client, ...renderWithProviders(<InboxColumn {...props} />, client) };
}

beforeEach(() => {
  vi.mocked(openCommandPalette).mockClear();
});

describe("InboxColumn: the reference's nav block", () => {
  it("renders the workspace name in the brand header", async () => {
    renderColumn();

    const brand = await screen.findByRole("button", { name: "Switch workspace" });
    expect(brand).toHaveTextContent("Acme Plumbing");
    // The round mark is the workspace INITIAL, not a truncation of the name.
    expect(brand.querySelector('span[aria-hidden="true"]')).toHaveTextContent("A");
  });

  it("Search opens the existing command palette", async () => {
    renderColumn();

    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(openCommandPalette).toHaveBeenCalledTimes(1);
  });

  it("mounts the existing notification bell", async () => {
    renderColumn();

    // NotificationBell's own trigger name - proof it is that component and not a new
    // bell drawn to match the mockup.
    expect(await screen.findByRole("button", { name: "Alerts" })).toBeInTheDocument();
  });

  it('groups the destinations under a "Workspace" heading', async () => {
    renderColumn();

    const nav = await screen.findByRole("navigation", { name: "Workspace" });
    expect(within(nav).getByText("Workspace")).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Contacts" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Campaigns" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Settings" })).toBeInTheDocument();
    // The rail IS the inbox - a link back to where you already are is noise.
    expect(within(nav).queryByRole("link", { name: "Inbox" })).not.toBeInTheDocument();
  });

  it("hides a gated item from a member without the permission", async () => {
    renderColumn({}, AGENT_CAPS);

    const nav = await screen.findByRole("navigation", { name: "Workspace" });
    // Contacts is held by the agent role, so its presence proves the nav rendered at
    // all and the Campaigns assertion below is not passing on an empty list.
    expect(within(nav).getByRole("link", { name: "Contacts" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Campaigns" })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
  });

  /**
   * THE rail contract, in one assertion: the operator asked for brand / Search /
   * Notifications / Workspace (Contacts, Campaigns, Settings) / Lines, in that order,
   * "and nothing else". This reads the accessible names off the Workspace nav in DOM
   * order, so a re-added row fails here rather than being noticed in a screenshot.
   */
  it("lists exactly Contacts, Campaigns, Settings under Workspace, in that order", async () => {
    renderColumn();

    const nav = await screen.findByRole("navigation", { name: "Workspace" });
    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.getAttribute("aria-label")),
    ).toEqual(["Contacts", "Campaigns", "Settings"]);
  });

  /**
   * The three destinations a previous pass carried here. They are NOT deleted - they moved
   * into the Settings surface (SettingsPage's section nav) - but the rail must not show
   * them, and this caller holds every permission each of them is gated on, so their
   * absence is the placement and not a gate quietly refusing.
   */
  it("no longer carries Calls, Setup or Trust & safety anywhere in the rail", async () => {
    renderColumn({}, {
      ...FULL_CAPS,
      // A workspace with work left on the checklist, so Setup WOULD be offered by
      // useRailNav - otherwise this test could pass because there was nothing to show.
      org: { ...FULL_CAPS.org, has_number: false },
    });

    // The nav has rendered before we assert absences.
    await screen.findByRole("link", { name: "Contacts" });
    for (const label of ["Calls", "Setup", "Trust & safety", "Inbox"]) {
      expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
  });

  it("useRailNav really would offer Setup for that workspace - the absence above is not vacuous", async () => {
    // Same capabilities, rendered through the icon Sidebar, which lists the full set.
    // If this ever stops finding Setup, the test above proves nothing.
    const { Sidebar } = await import("@/components/shell/Sidebar");
    const client = makeStubClient({
      "/api/v1/auth/me": ME,
      "/api/v1/me/capabilities": {
        ...FULL_CAPS,
        org: { ...FULL_CAPS.org, has_number: false },
      },
      "/api/v1/notifications": { items: [], unread_count: 0 },
    });
    renderWithProviders(<Sidebar />, client);

    expect(await screen.findByRole("link", { name: "Setup" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Calls" })).toBeInTheDocument();
  });

  /**
   * The regression the trim could have caused. <Sidebar/> is hidden on /inbox, so if Sign
   * out came out of this rail with the rest of the icon rail's cargo there would be no way
   * to sign out on the page users spend all day on. It lives in the cluster beneath the
   * lines - the "settings access below the phone numbers" - alongside the theme toggle.
   */
  it("keeps Sign out and the theme toggle reachable, below the lines", async () => {
    renderColumn();

    const cluster = await screen.findByRole("group", { name: "Settings and account" });
    expect(within(cluster).getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    // ThemeToggle's own accessible name, whichever way round the stored theme is.
    expect(
      within(cluster).getByRole("button", {
        name: /^Switch to the (light|dark) theme$/,
      }),
    ).toBeInTheDocument();
  });

  it("signing out still ends the session rather than merely rendering a button", async () => {
    const { client } = renderColumn();

    const cluster = await screen.findByRole("group", { name: "Settings and account" });
    await userEvent.click(within(cluster).getByRole("button", { name: "Sign out" }));

    // AuthContext.logout POSTs to the session endpoint and clears the token. A
    // decorative button would leave both untouched.
    expect(client.calls.map((c) => c.path)).toContain("/api/v1/auth/logout");
    expect(client.auth.token).toBeNull();
  });

  /**
   * Trust & safety's gate is `is_platform_operator`, which no settings permission implies.
   * A platform operator who is only an agent here gets no Settings row, and /settings
   * bounces such a caller back to /inbox - so without this fallback /ops would have no door
   * on the inbox at all.
   */
  it("gives a platform operator with no settings access a direct Trust & safety row", async () => {
    renderColumn({}, AGENT_CAPS, { ...ME, is_platform_operator: true });

    const nav = await screen.findByRole("navigation", { name: "Workspace" });
    expect(within(nav).getByRole("link", { name: "Trust & safety" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
  });

  it("but an operator who CAN open Settings gets Settings only - never both", async () => {
    renderColumn({}, FULL_CAPS, { ...ME, is_platform_operator: true });

    const nav = await screen.findByRole("navigation", { name: "Workspace" });
    expect(within(nav).getByRole("link", { name: "Settings" })).toBeInTheDocument();
    expect(
      within(nav).queryByRole("link", { name: "Trust & safety" }),
    ).not.toBeInTheDocument();
  });

  it("shows no Trust & safety row to a non-operator, whatever their permissions", async () => {
    renderColumn({}, AGENT_CAPS);

    await screen.findByRole("navigation", { name: "Workspace" });
    expect(
      screen.queryByRole("link", { name: "Trust & safety" }),
    ).not.toBeInTheDocument();
  });

  it("renders no navigation at all while capabilities are still loading", async () => {
    renderColumn({}, new Promise<never>(() => undefined));

    const nav = await screen.findByRole("navigation", { name: "Workspace" });
    expect(nav).toHaveAttribute("aria-busy", "true");
    expect(within(nav).queryAllByRole("link")).toHaveLength(0);
  });
});

describe("InboxColumn: the Lines group", () => {
  it('renders "All conversations"', () => {
    renderColumn();

    expect(
      screen.getByRole("button", { name: "All conversations" }),
    ).toBeInTheDocument();
  });

  it("renders one row per inbox with its name", () => {
    renderColumn();

    const nav = screen.getByRole("navigation", { name: "Inboxes" });
    expect(within(nav).getByText("Sales")).toBeInTheDocument();
    expect(within(nav).getByText("Support")).toBeInTheDocument();
  });

  it("a line row shows the name, the formatted number and the initials avatar", () => {
    renderColumn();

    const row = screen.getByRole("button", { name: "Sales" });
    expect(within(row).getByText("Sales")).toBeInTheDocument();
    // formatPhone, not a hand-rolled format: +14694617576 -> (469) 461-7576.
    expect(within(row).getByText("(469) 461-7576")).toBeInTheDocument();
    // First letter + next consonant, the reference's scheme (Sales -> SL).
    expect(within(row).getByText("SL")).toBeInTheDocument();
  });

  /**
   * WAS "the avatar carries the inbox colour" and asserted `backgroundColor` directly.
   * The avatar is now a GRADIENT disc rather than a flat fill - the reference draws every
   * `.av` as `linear-gradient(145deg, hue, darker-hue)` - so the colour is handed to
   * `.cx-line-avatar` in consoleTheme.css as the `--cx-line-av` custom property and that
   * rule builds the ramp. The contract this test defends is unchanged: THE INBOX'S OWN
   * COLOUR, and no other, is what paints the avatar. Only where it is written moved.
   *
   * It asserts the custom property off the style attribute rather than through
   * toHaveStyle: jsdom's CSSStyleDeclaration does not surface custom properties to it.
   */
  it("the avatar is painted from the inbox colour", () => {
    renderColumn();

    const row = screen.getByRole("button", { name: "Sales" });
    const avatar = row.querySelector('span[aria-hidden="true"]');
    expect(avatar).toHaveTextContent("SL");
    expect(avatar).toHaveClass("cx-line-avatar");
    expect(avatar?.getAttribute("style")).toContain("--cx-line-av: #22c55e");
  });

  /**
   * THE ROW HEIGHT REGRESSION GUARD, and an honest account of what it can and cannot do.
   *
   * Button's size="sm" variant is `h-8 px-3 text-xs` - a FIXED 32px. This row stacks a
   * 13px name over an 11.5px number, which with the reference's 9px padding wants ~52px,
   * so without an override the row clamped ~50px of content into 32px and the number sat
   * jammed against the name. `h-auto` (cn is tailwind-merge, so it beats the variant)
   * is the fix.
   *
   * vitest runs with `css: false`: NO test in this file can measure a height, and this one
   * does not pretend to. It asserts the class is present, which is a weaker claim than "the
   * row is 52px tall" - but it is not vacuous, because deleting `h-auto` is exactly how the
   * bug comes back and this goes red when that happens.
   *
   * The real measurement was taken out of band, in headless Chrome against the production
   * CSS bundle and this component's actual rendered markup:
   *   before (h-8, py-2):     rowHeight 32.00px  <- content clipped
   *   after  (h-auto, py-9px): rowHeight 51.56px = 9 + 16.89 (name) + 16.67 (number) + 9
   * which is the reference `.ln`'s ~52px.
   */
  it("does not let the button variant clamp the row to a single line's height", () => {
    renderColumn();

    const row = screen.getByRole("button", { name: "Sales" });
    // The override, and the absence of the fixed height it overrides.
    expect(row.className).toContain("h-auto");
    expect(row.className.split(/\s+/)).not.toContain("h-8");
    // The name and the number are a STACKED block, not two spans on one line - a row that
    // laid them out in a row would fit in 32px and never need the override above.
    const stack = row.querySelector("span.flex-col");
    expect(stack).not.toBeNull();
    expect(within(stack as HTMLElement).getByText("Sales")).toBeInTheDocument();
    expect(within(stack as HTMLElement).getByText("(469) 461-7576")).toBeInTheDocument();
  });

  /** The reference's `.ln` padding is `9px 10px`. Same caveat: the class, not the pixels. */
  it("uses the reference's row padding rather than the variant's", () => {
    renderColumn();

    const row = screen.getByRole("button", { name: "Sales" });
    expect(row.className).toContain("py-[9px]");
    expect(row.className).toContain("px-2.5");
  });

  it("an unformattable number is shown as-is rather than mangled", () => {
    renderColumn({ inboxes: [inbox({ name: "London", e164: "+442070000123" })] });

    const row = screen.getByRole("button", { name: "London" });
    expect(within(row).getByText("+442070000123")).toBeInTheDocument();
  });

  it("an unread count badge appears only when > 0", () => {
    renderColumn({ unread: {} });
    expect(screen.queryByLabelText("2 unread")).not.toBeInTheDocument();

    renderColumn({ unread: { i2: 2 } });
    expect(screen.getByLabelText("2 unread")).toHaveTextContent("2");
  });

  it("a truncated count renders as N+ with a truthful title", () => {
    renderColumn({ unread: { i1: 8 }, unreadTruncated: true });

    const badge = screen.getByLabelText("8 unread");
    expect(badge).toHaveTextContent("8+");
    expect(badge).toHaveAttribute(
      "title",
      "Only the most recent unread conversations are counted",
    );
  });

  // The Important/Unresponded/Snoozed/Overdue rows moved OUT of the rail and are now
  // only the chips in ConversationList (see p26Filters.test.tsx). The rail sets scope,
  // never a filter - this pins that, so a re-added row is a failing test rather than a
  // second control over one value.
  it("offers no filter rows - the lines group is scope-only", () => {
    renderColumn();

    for (const label of ["Important", "Unresponded", "Snoozed", "Overdue"]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
  });

  it("every row in the lines group selects a scope, never a filter", async () => {
    const onSelect = vi.fn();
    renderColumn({ onSelect });

    const nav = screen.getByRole("navigation", { name: "Inboxes" });
    // Every button EXCEPT the per-line "Manage access to ..." control, which is not a row and
    // does not select anything - it opens the access drawer.
    //
    // The loop used to click literally every button, and rendered no access control only
    // because this fixture omits `inboxes:admin`.
    //
    // Measured, not assumed: removing this filter AND adding that capability still passes
    // 31/31, because clicking the access button never calls `onSelect` and the assertion
    // below only inspects `onSelect` calls. So this is not fixing a latent failure - it is
    // stopping the loop from opening a drawer and firing its unstubbed queries as a side
    // effect, which is noise rather than a red test. Keep it for that reason, not because
    // the test would otherwise break.
    //
    // The teeth are unchanged either way: a re-added FILTER row is still clicked, and still
    // fails the `kind` assertion below.
    const rows = within(nav)
      .getAllByRole("button")
      .filter((b) => !/^Manage access to /.test(b.getAttribute("aria-label") ?? ""));
    for (const button of rows) {
      await userEvent.click(button);
    }

    expect(onSelect).toHaveBeenCalled();
    for (const [selection] of onSelect.mock.calls) {
      expect(["all", "inbox"]).toContain(selection.kind);
    }
  });

  it('groups the numbers under a "Lines" heading', () => {
    renderColumn();

    const nav = screen.getByRole("navigation", { name: "Inboxes" });
    expect(within(nav).getByText("Lines")).toBeInTheDocument();
  });

  it('the selected row carries aria-current="true"', () => {
    renderColumn({ selection: { kind: "inbox", inboxId: "i1" } });

    expect(screen.getByRole("button", { name: "Sales" })).toHaveAttribute(
      "aria-current",
      "true",
    );
  });

  it('"+ New" is disabled with the read-only title when canCompose is false', () => {
    renderColumn({
      onNew: vi.fn(),
      canCompose: false,
      canComposeLoading: false,
    });

    const newButton = screen.getByRole("button", { name: "New conversation" });
    expect(newButton).toBeDisabled();
    expect(newButton).toHaveAttribute(
      "title",
      "Read-only inbox — you can view but not start new conversations",
    );
  });

  it("the no-inbox empty state does not repeat the conversation list's sentence", () => {
    renderColumn({ inboxes: [], isLoading: false, error: null });

    expect(screen.getByText("No inboxes yet")).toBeInTheDocument();
    // The teaching sentence lives in ONE place - the conversation list - so the two
    // columns never show the same error twice.
    expect(
      screen.queryByText("You have no inbox access yet — ask an admin"),
    ).not.toBeInTheDocument();
  });
});

describe("lineInitials", () => {
  it("matches the reference's three line avatars", () => {
    // docs/design/console-reference.html: Main line -> MN, Support -> SP, Sales -> SL.
    expect(lineInitials("Main line")).toBe("MN");
    expect(lineInitials("Support")).toBe("SP");
    expect(lineInitials("Sales")).toBe("SL");
  });

  it("falls back sanely on an all-vowel or unnameable line", () => {
    expect(lineInitials("Ai")).toBe("AI");
    expect(lineInitials("A")).toBe("AA");
    expect(lineInitials("+44 20")).toBe("??");
  });
});
