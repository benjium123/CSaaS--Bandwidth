import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "./client";

// One error-message helper for both domains; it lives in api/contacts.ts and is
// re-exported here so role screens don't reach across domains for it.
export { getErrorMessage } from "./contacts";

export type RoleOut = {
  id: string;
  name: string;
  permissions: string[];
  is_system: boolean;
  member_count: number;
};

export const ROLES_QUERY_KEY = ["roles"] as const;

export function useRoles(api: ApiClient, enabled = true) {
  return useQuery({
    queryKey: ROLES_QUERY_KEY,
    queryFn: () => api.request<RoleOut[]>("/api/v1/roles"),
    enabled,
  });
}

export function useCreateRole(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { name: string; permissions: string[]; clone_from?: string }) =>
      api.request<RoleOut>("/api/v1/roles", { method: "POST", json: vars }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ROLES_QUERY_KEY });
    },
  });
}

export function useUpdateRole(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string; name?: string; permissions?: string[] }) => {
      const { id, ...body } = vars;
      return api.request<RoleOut>(`/api/v1/roles/${id}`, { method: "PATCH", json: body });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ROLES_QUERY_KEY });
      qc.invalidateQueries({ queryKey: ["org-members"] });
    },
  });
}

export function useDeleteRole(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { id: string }) =>
      api.request<void>(`/api/v1/roles/${vars.id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ROLES_QUERY_KEY });
      qc.invalidateQueries({ queryKey: ["org-members"] });
    },
  });
}

export type PermissionGroup = {
  resource: string;
  label: string;
  permissions: { key: string; label: string; hint?: string }[];
};

export const PERMISSION_GROUPS: PermissionGroup[] = [
  {
    resource: "workspace",
    label: "Workspace",
    permissions: [
      { key: "org:read", label: "Can see workspace settings" },
      { key: "org:update", label: "Can change workspace settings" },
    ],
  },
  {
    resource: "people",
    label: "People",
    permissions: [
      { key: "members:read", label: "Can see the team list" },
      { key: "members:invite", label: "Can invite people" },
      { key: "members:update", label: "Can change someone's role" },
      { key: "members:remove", label: "Can remove people" },
      { key: "roles:read", label: "Can see roles" },
      { key: "roles:write", label: "Can create and change roles" },
    ],
  },
  {
    resource: "inbox",
    label: "Inbox",
    permissions: [
      { key: "inbox:read", label: "Can see the inbox" },
      { key: "inbox:send", label: "Can send & call" },
      { key: "inbox:manage", label: "Can manage their own inboxes" },
      { key: "inboxes:admin", label: "Can manage every inbox" },
    ],
  },
  {
    resource: "departments",
    label: "Teams",
    permissions: [
      { key: "departments:read", label: "Can see teams" },
      { key: "departments:manage", label: "Can manage teams" },
    ],
  },
  {
    resource: "contacts",
    label: "Contacts",
    permissions: [
      { key: "contacts:read", label: "Can see contacts" },
      {
        key: "contacts:read_all",
        label: "Can see all contacts (ignores the team rule)",
      },
      { key: "contacts:write", label: "Can add and edit contacts" },
      { key: "contacts:assign", label: "Can reassign contacts" },
    ],
  },
  {
    resource: "numbers",
    label: "Phone numbers",
    permissions: [
      { key: "numbers:read", label: "Can see phone numbers" },
      { key: "numbers:manage", label: "Can add and remove numbers" },
    ],
  },
  {
    resource: "campaigns",
    label: "Campaigns",
    permissions: [
      { key: "campaigns:read", label: "Can see campaigns" },
      { key: "campaigns:manage", label: "Can run campaigns" },
    ],
  },
  {
    resource: "calls",
    label: "Calls",
    permissions: [
      { key: "calls:read", label: "Can see call history" },
      { key: "calls:place", label: "Can place calls" },
      { key: "calls:supervise", label: "Can listen in on live calls" },
    ],
  },
  {
    resource: "reports",
    label: "Reporting",
    permissions: [{ key: "reports:read", label: "Can see reports" }],
  },
  {
    resource: "settings",
    label: "Settings",
    permissions: [
      { key: "settings:read", label: "Can see settings" },
      { key: "settings:write", label: "Can change settings" },
    ],
  },
  {
    resource: "messaging",
    label: "Messaging",
    permissions: [
      { key: "compliance:read", label: "Can see opt-outs and consent" },
      { key: "compliance:manage", label: "Can change opt-out rules" },
      { key: "templates:read", label: "Can use message templates" },
      { key: "templates:manage", label: "Can create message templates" },
    ],
  },
];

// These are owner-only and are intentionally not offered in the matrix; the
// backend rejects them on custom roles, so offering them would be a trap.
export const OWNER_ONLY_PERMISSIONS = ["org:delete", "org:billing"] as const;

export const STARTING_POINTS = [
  { id: "agent", label: "Agent", description: "Works the inbox and contacts." },
  { id: "admin", label: "Admin", description: "Runs the workspace, except billing." },
  { id: "blank", label: "Blank", description: "Start with nothing selected." },
];

/**
 * Resolve a starting point against the already-loaded role list. Sending the
 * explicit permissions array keeps it authoritative, while clone_from lets the
 * backend copy any future housekeeping fields it adds to system roles.
 */
export function startingPointPermissions(
  roles: RoleOut[],
  startingPoint: string,
): { permissions: string[]; clone_from?: string } {
  const targetName =
    startingPoint === "agent" ? "agent" : startingPoint === "admin" ? "admin" : null;
  if (!targetName) return { permissions: [] };

  const role = roles.find((r) => r.is_system && r.name === targetName);
  if (!role) return { permissions: [] };

  return { permissions: [...role.permissions], clone_from: role.id };
}
