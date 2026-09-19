import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { fetchDepartments, fetchOrgMembers } from "@/api/conversations";
import {
  getErrorMessage,
  useAssignContactOwner,
  useBulkAssignContacts,
  type ContactOut,
} from "@/api/contacts";
import { InitialsAvatar } from "@/components/ui/consoleChrome";
import { Button, Spinner } from "@/components/ui/primitives";

export function AssignOwnerDrawer({
  contacts,
  onClose,
}: {
  contacts: ContactOut[];
  onClose: () => void;
}) {
  const { api } = useAuth();
  const closeButtonRef = React.useRef<HTMLButtonElement>(null);
  const closeTimer = React.useRef<number | null>(null);

  const membersQuery = useQuery({
    queryKey: ["org-members"],
    queryFn: () => fetchOrgMembers(api),
  });
  const departmentsQuery = useQuery({
    queryKey: ["departments"],
    queryFn: () => fetchDepartments(api),
  });

  const assignOwner = useAssignContactOwner(api);
  const bulkAssign = useBulkAssignContacts(api);

  const [ownerId, setOwnerId] = React.useState("");
  const [deptId, setDeptId] = React.useState("");
  const [saveError, setSaveError] = React.useState<string | null>(null);
  const [saved, setSaved] = React.useState(false);
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    closeButtonRef.current?.focus();

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (closeTimer.current) window.clearTimeout(closeTimer.current);
    };
  }, [onClose]);

  const loading = membersQuery.isPending || departmentsQuery.isPending;
  const loadError = membersQuery.error || departmentsQuery.error;
  const members = membersQuery.data ?? [];
  const departments = departmentsQuery.data ?? [];
  // Presentation only: which face the picker draws. It reads the same `ownerId` the select
  // already holds and changes nothing about what gets saved.
  const selectedOwner = members.find((member) => member.user_id === ownerId);

  function retry() {
    membersQuery.refetch();
    departmentsQuery.refetch();
  }

  async function handleSave() {
    setSaveError(null);
    setSaved(false);
    setSaving(true);
    try {
      if (contacts.length === 1) {
        await assignOwner.mutateAsync({
          contactId: contacts[0].id,
          owner_user_id: ownerId || null,
          department_id: deptId || null,
        });
      } else {
        await bulkAssign.mutateAsync({
          contact_ids: contacts.map((contact) => contact.id),
          owner_user_id: ownerId || null,
          department_id: deptId || null,
        });
      }

      setSaved(true);
      closeTimer.current = window.setTimeout(onClose, 400);
    } catch (err) {
      setSaveError(getErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  const title =
    contacts.length === 1 ? "Assign contact" : `Assign ${contacts.length} contacts`;

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/50">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        // 18px on the two corners that face into the page only — the same treatment the
        // Drawer primitive gives its panel. The outer corners are flush against the
        // viewport edge and rounding them would show the page through.
        className="h-full w-full max-w-md space-y-[14px] overflow-y-auto rounded-l-[var(--cx-r-lg,18px)] border-l border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))] p-[18px] shadow-[var(--cx-shadow)]"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-[17px] font-semibold tracking-[-0.015em] text-[hsl(var(--cx-text))]">
            {title}
          </h2>
          <Button
            ref={closeButtonRef}
            type="button"
            variant="ghost"
            size="icon"
            aria-label="Close assign drawer"
            onClick={onClose}
          >
            ×
          </Button>
        </div>

        {loading ? (
          <Spinner />
        ) : loadError ? (
          <div className="space-y-2">
            <p role="alert" className="text-sm text-destructive">
              {getErrorMessage(loadError)}
            </p>
            <Button type="button" size="sm" variant="outline" onClick={retry}>
              Retry
            </Button>
          </div>
        ) : (
          <div className="space-y-[14px]">
            <div className="space-y-[6px]">
              <label
                className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]"
                htmlFor="assign-owner"
              >
                Owner
              </label>
              {/* The picker STAYS a native <select> — it is what the suites drive and what
                  a long member list is actually usable with. The face is a read-out of the
                  current choice beside it, so an owner picker shows a person the way every
                  other person-row on the console does, without swapping the control out. */}
              <div className="flex items-center gap-[11px]">
                {selectedOwner ? (
                  <InitialsAvatar
                    name={selectedOwner.full_name}
                    seed={selectedOwner.user_id}
                    size="md"
                  />
                ) : (
                  // Unassigned has no initials to draw, so it takes the same circle as an
                  // empty well rather than a monogram of nothing.
                  <span
                    aria-hidden="true"
                    className="h-[34px] w-[34px] flex-none rounded-full border border-dashed border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))]"
                  />
                )}
                <select
                  id="assign-owner"
                  className="h-[38px] min-w-0 flex-1 rounded-[var(--cx-r-sm,12px)] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-[11px] text-[13.5px] text-[hsl(var(--cx-text))]"
                  value={ownerId}
                  onChange={(event) => setOwnerId(event.target.value)}
                  disabled={saving}
                >
                  <option value="">Unassigned</option>
                  {members.map((member) => (
                    <option key={member.user_id} value={member.user_id}>
                      {member.full_name}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="space-y-[6px]">
              <label
                className="block text-[11.5px] font-semibold text-[hsl(var(--cx-muted))]"
                htmlFor="assign-team"
              >
                Team
              </label>
              <select
                id="assign-team"
                className="h-[38px] w-full rounded-[var(--cx-r-sm,12px)] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-[11px] text-[13.5px] text-[hsl(var(--cx-text))]"
                value={deptId}
                onChange={(event) => setDeptId(event.target.value)}
                disabled={saving}
              >
                <option value="">No team</option>
                {departments.map((department) => (
                  <option key={department.id} value={department.id}>
                    {department.name}
                  </option>
                ))}
              </select>
            </div>

            {saveError && (
              <p role="alert" className="text-sm text-destructive">
                {saveError}
              </p>
            )}
            {saved && <p className="text-sm text-[hsl(var(--cx-live))]">Saved</p>}

            <div className="flex gap-[11px]">
              <Button type="button" onClick={handleSave} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </Button>
              <Button type="button" variant="outline" onClick={onClose}>
                Cancel
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
