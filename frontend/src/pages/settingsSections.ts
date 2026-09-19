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
  | "developers";

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
};

export const SETTINGS_SECTIONS: SettingsSection[] = [
  { id: "workspace", label: "Workspace", permission: "org:read" },
  { id: "verification", label: "Business verification", permission: "org:read", ownerOnly: true },
  { id: "team", label: "Team", permission: "members:read" },
  { id: "inboxes", label: "Departments & inboxes", permission: "inboxes:admin" },
  { id: "numbers", label: "Phone numbers", permission: "numbers:read" },
  { id: "providers", label: "Providers", permission: "settings:read" },
  { id: "messaging", label: "Messaging", permission: "compliance:manage" },
  { id: "calling", label: "Calling", permission: "settings:read" },
  { id: "ai", label: "AI", permission: "settings:read" },
  { id: "billing", label: "Billing & usage", permission: "settings:read", ownerOnly: true },
  { id: "developers", label: "Developers", permission: "settings:write" },
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
  return can(section.permission) && (!section.ownerOnly || owner);
}
