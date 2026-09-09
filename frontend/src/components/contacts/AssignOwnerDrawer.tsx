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
        className="h-full w-full max-w-md space-y-4 overflow-y-auto border-l border-border bg-background p-6 shadow-xl"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-base font-semibold">{title}</h2>
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
          <div className="space-y-4">
            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor="assign-owner">
                Owner
              </label>
              <select
                id="assign-owner"
                className="h-9 rounded-md border border-border bg-background px-2 text-sm"
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

            <div className="space-y-1">
              <label className="block text-xs text-muted-foreground" htmlFor="assign-team">
                Team
              </label>
              <select
                id="assign-team"
                className="h-9 rounded-md border border-border bg-background px-2 text-sm"
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
            {saved && <p className="text-sm text-green-400">Saved</p>}

            <div className="flex gap-2">
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
