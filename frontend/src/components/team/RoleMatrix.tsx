import { PERMISSION_GROUPS } from "@/api/roles";

const ORDERED_KEYS = PERMISSION_GROUPS.flatMap((group) =>
  group.permissions.map((permission) => permission.key),
);

export function RoleMatrix({
  value,
  onChange,
  readOnly = false,
  grantablePermissions,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  readOnly?: boolean;
  grantablePermissions: (key: string) => boolean;
}) {
  function toggle(key: string) {
    if (readOnly) return;
    const next = value.includes(key)
      ? value.filter((item) => item !== key)
      : [...value, key];
    const known = ORDERED_KEYS.filter((item) => next.includes(item));
    const unknown = next.filter((item) => !ORDERED_KEYS.includes(item));
    onChange([...known, ...unknown]);
  }

  return (
    <div className="space-y-4">
      {readOnly && (
        <p className="text-sm text-muted-foreground">Built-in roles can't be edited.</p>
      )}

      {PERMISSION_GROUPS.map((group) => (
        <fieldset key={group.resource}>
          <legend className="text-sm font-medium">{group.label}</legend>
          <div className="mt-2 grid gap-2">
            {group.permissions.map((permission) => {
              const checked = value.includes(permission.key);
              const canGrant = grantablePermissions(permission.key);
              const disabled = readOnly || !canGrant;
              const title = !canGrant
                ? "You don't have this permission yourself, so you can't grant it."
                : undefined;
              const id = `role-perm-${permission.key.replace(":", "-")}`;

              return (
                <label
                  key={permission.key}
                  htmlFor={id}
                  className="flex items-center gap-2 text-sm"
                  title={title}
                >
                  <input
                    id={id}
                    type="checkbox"
                    checked={checked}
                    disabled={disabled}
                    onChange={() => toggle(permission.key)}
                    title={title}
                  />
                  {permission.label}
                </label>
              );
            })}
          </div>
        </fieldset>
      ))}
    </div>
  );
}
