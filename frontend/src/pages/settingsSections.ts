// Lives apart from SettingsPage.tsx so Sidebar can gate the Settings gear without pulling in every settings page module.
export type SettingsSectionId =
  | "workspace"
  | "team"
  | "inboxes"
  | "numbers"
  | "providers"
  | "messaging"
  | "calling"
  | "ai"
  | "billing"
  | "developers";

export const SETTINGS_SECTIONS: {
  id: SettingsSectionId;
  label: string;
  permission: string;
}[] = [
  { id: "workspace", label: "Workspace", permission: "org:read" },
  { id: "team", label: "Team", permission: "members:read" },
  { id: "inboxes", label: "Departments & inboxes", permission: "inboxes:admin" },
  { id: "numbers", label: "Phone numbers", permission: "numbers:read" },
  { id: "providers", label: "Providers", permission: "settings:read" },
  { id: "messaging", label: "Messaging", permission: "compliance:manage" },
  { id: "calling", label: "Calling", permission: "settings:read" },
  { id: "ai", label: "AI", permission: "settings:read" },
  { id: "billing", label: "Billing & usage", permission: "settings:read" },
  { id: "developers", label: "Developers", permission: "settings:write" },
];
