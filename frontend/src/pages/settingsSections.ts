// Lives apart from SettingsPage.tsx so Sidebar can gate the Settings gear without pulling in every settings page module.
export type SettingsSectionId =
  | "workspace"
  | "verification"
  | "team"
  | "inboxes"
  | "numbers"
  | "providers"
  | "messaging"
  | "calling"
  | "ai"
  | "billing"
  | "developers"
  | "profile"
  | "notifications";

/** Phase 1c (docs/design/INBOX_NAV_SPEC.md §3): the menu is grouped, not one flat list. */
export type SettingsGroup = "phone" | "workspace" | "account";

export const SETTINGS_GROUPS: { id: SettingsGroup; label: string }[] = [
  { id: "phone", label: "Phone system" },
  { id: "workspace", label: "Workspace" },
  { id: "account", label: "Your account" },
];

/** One row of the settings nav. `ownerOnly` marks a section only the workspace OWNER may
 * open even when the caller holds the section's permission string: a non-owner ADMIN holds
 * both `settings:read` and `org:read`, so `permission` alone cannot express this.
 * Ownership is not in the permission list at all (the server expands the owner's wildcard
 * into every explicit string), so it is read from the membership via isOwner() in
 * auth/AuthContext.tsx. The server enforces it either way. */
export type SettingsSection = {
  id: SettingsSectionId;
  label: string;
  permission: string;
  ownerOnly?: boolean;
  group: SettingsGroup;
  /** Listed in the menu only for platform operators (us). The route itself keeps its own
   * permission gate; hiding it from customers is a menu decision, not an access rule. */
  operatorOnly?: boolean;
  /** The section's own tabs (the `?tab=` ids its component understands), listed under it
   * in the menu while it is open. */
  subPages?: { id: string; label: string }[];
};

// Array order is the order SettingsIndexRedirect tries sections in; the MENU order comes
// from SETTINGS_GROUPS + the order within each group below.
export const SETTINGS_SECTIONS: SettingsSection[] = [
  {
    id: "workspace", label: "Workspace", permission: "org:read", group: "workspace",
    subPages: [{ id: "general", label: "General" }, { id: "data", label: "Data" }],
  },
  { id: "verification", label: "Business verification", permission: "org:read", ownerOnly: true, group: "workspace" },
  {
    id: "team", label: "Team & security", permission: "members:read", group: "workspace",
    subPages: [{ id: "members", label: "Members & roles" }, { id: "security", label: "Security" }],
  },
  { id: "inboxes", label: "Departments", permission: "inboxes:admin", group: "phone" },
  { id: "numbers", label: "Lines", permission: "numbers:read", group: "phone" },
  { id: "providers", label: "Providers", permission: "settings:read", group: "workspace", operatorOnly: true },
  { id: "messaging", label: "Messaging", permission: "compliance:manage", group: "phone" },
  {
    id: "calling", label: "Calling", permission: "settings:read", group: "phone",
    subPages: [
      { id: "general", label: "Recording & results" },
      { id: "flows", label: "Call flows" },
      { id: "queues", label: "Queues" },
    ],
  },
  {
    id: "ai", label: "AI assistants", permission: "settings:read", group: "phone",
    subPages: [
      { id: "assistants", label: "Assistants" },
      { id: "providers", label: "Providers" },
      { id: "knowledge", label: "Knowledge" },
      { id: "appointments", label: "Appointments" },
    ],
  },
  {
    id: "billing", label: "Billing & usage", permission: "settings:read", ownerOnly: true, group: "workspace",
    subPages: [
      { id: "credits", label: "Credits" },
      { id: "usage", label: "Usage" },
      { id: "dashboard", label: "Dashboard" },
    ],
  },
  { id: "developers", label: "Developers", permission: "settings:write", group: "workspace", operatorOnly: true },
  // Every member: their own name and 911 address. Empty permission = no gate.
  { id: "profile", label: "My profile", permission: "", group: "account" },
  // Every member: desktop alerts, ringtone, microphone and speaker.
  { id: "notifications", label: "Notifications & sound", permission: "", group: "account" },
];

/** Menu order within each group (sections not listed keep array order at the end). */
export const SETTINGS_MENU_ORDER: SettingsSectionId[] = [
  "numbers", "inboxes", "calling", "messaging", "ai",
  "workspace", "team", "verification", "billing", "providers", "developers",
  "profile", "notifications",
];

/** The one statement of "may this caller open this section?", shared by SettingsPage's nav
 * and deep-link gate and by SettingsIndexRedirect. It is AND, not OR: an ownerOnly section
 * still requires its permission string - ownership is an ADDITIONAL requirement, never a
 * substitute for the permission. */
export function canViewSettingsSection(
  section: SettingsSection,
  can: (permission: string) => boolean,
  owner: boolean,
): boolean {
  return (section.permission === "" || can(section.permission)) && (!section.ownerOnly || owner);
}
