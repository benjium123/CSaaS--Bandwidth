import * as React from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { hasPermission, useAuth } from "@/auth/AuthContext";
import { fetchDepartments, fetchOrgMembers } from "@/api/conversations";
import {
  getErrorMessage,
  useContacts,
  useCreateContact,
  type ContactFilter,
  type ContactOut,
} from "@/api/contacts";
import { AssignOwnerDrawer } from "@/components/contacts/AssignOwnerDrawer";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { PhoneNumberMenu } from "@/components/ui/PhoneNumberMenu";
import { formatPhone } from "@/lib/format";

export function ContactsPage() {
  const { api, me, orgId } = useAuth();
  const navigate = useNavigate();
  const [q, setQ] = React.useState("");
  const [filter, setFilter] = React.useState<ContactFilter | null>(null);
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [assigning, setAssigning] = React.useState<ContactOut[] | null>(null);

  const contactsQuery = useContacts(api, { q, filter });
  const createContact = useCreateContact(api);
  const membersQuery = useQuery({
    queryKey: ["org-members"],
    queryFn: () => fetchOrgMembers(api),
  });
  const departmentsQuery = useQuery({
    queryKey: ["departments"],
    queryFn: () => fetchDepartments(api),
  });

  const [name, setName] = React.useState("");
  const [phone, setPhone] = React.useState("");
  const [addError, setAddError] = React.useState<string | null>(null);
  const [addedName, setAddedName] = React.useState<string | null>(null);

  const canAssign = hasPermission(me, orgId, "contacts:assign");
  const contacts = contactsQuery.data ?? [];

  const memberById = React.useMemo(
    () => new Map((membersQuery.data ?? []).map((member) => [member.user_id, member.full_name])),
    [membersQuery.data],
  );
  const departmentById = React.useMemo(
    () => new Map((departmentsQuery.data ?? []).map((dept) => [dept.id, dept.name])),
    [departmentsQuery.data],
  );

  React.useEffect(() => {
    setSelected(new Set());
  }, [q, filter]);

  function handleText(e164: string) {
    // `?compose=<e164>` starts a NEW conversation with this number. A ContactsPage
    // row has no way to know whether a conversation already exists, and `?contact=`
    // only selects an EXISTING thread.
    navigate(`/inbox?compose=${encodeURIComponent(e164)}`);
  }

  function toggleSelected(id: string) {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function openAssignDrawer(rows: ContactOut[]) {
    if (!canAssign) return;
    setAssigning(rows);
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setAddError(null);
    try {
      const createdContact = await createContact.mutateAsync({
        display_name: name,
        phones: phone ? [{ e164: phone, label: "mobile", is_primary: true }] : [],
      });
      setAddError(null);
      setAddedName(createdContact.display_name);
      setName("");
      setPhone("");
    } catch (err) {
      setAddError(getErrorMessage(err));
    }
  }

  const allSelected =
    contacts.length > 0 && contacts.every((contact) => selected.has(contact.id));

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-6">
      <h1 className="text-lg font-semibold">Contacts</h1>

      <form className="flex flex-wrap gap-2" onSubmit={create}>
        <Input
          aria-label="Contact name"
          placeholder="Name"
          className="flex-1"
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            setAddedName(null);
          }}
        />
        <Input
          aria-label="Contact phone"
          placeholder="+19725550199"
          className="flex-1"
          value={phone}
          onChange={(event) => {
            setPhone(event.target.value);
            setAddedName(null);
          }}
        />
        <Button type="submit" disabled={!name.trim() || createContact.isPending}>
          {createContact.isPending ? "Adding…" : "Add"}
        </Button>
      </form>

      {addError && (
        <p role="alert" className="text-sm text-destructive">
          {addError}
        </p>
      )}

      {addedName && <p className="text-sm text-green-400">Added {addedName}.</p>}

      <Input
        aria-label="Search contacts"
        placeholder="Search name or number"
        value={q}
        onChange={(event) => setQ(event.target.value)}
      />

      <div className="flex gap-2">
        {(["mine", "team", "unowned"] as const).map((item) => {
          const label =
            item === "mine" ? "Mine" : item === "team" ? "My team" : "Unowned";
          const active = filter === item;
          return (
            <Button
              key={item}
              type="button"
              size="sm"
              variant={active ? "default" : "outline"}
              aria-pressed={active}
              onClick={() => setFilter((previous) => (previous === item ? null : item))}
            >
              {label}
            </Button>
          );
        })}
      </div>

      {selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-border px-3 py-2 text-sm">
          <span>{selected.size} selected</span>
          <Button
            type="button"
            size="sm"
            disabled={!canAssign}
            title={!canAssign ? "You don't have permission to reassign contacts." : undefined}
            onClick={() => openAssignDrawer(contacts.filter((contact) => selected.has(contact.id)))}
          >
            Assign selected
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => setSelected(new Set())}
          >
            Clear selection
          </Button>
        </div>
      )}

      {contactsQuery.isPending ? (
        <Spinner />
      ) : contactsQuery.isError ? (
        <div className="space-y-2">
          <p role="alert" className="text-sm text-destructive">
            {getErrorMessage(contactsQuery.error)}
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => contactsQuery.refetch()}
          >
            Retry
          </Button>
        </div>
      ) : contacts.length === 0 ? (
        <div className="rounded-md border border-border p-6 text-sm text-muted-foreground">
          {q || filter ? (
            <div className="flex items-center gap-2">
              <span>No contacts match this filter.</span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => {
                  setQ("");
                  setFilter(null);
                }}
              >
                Clear it
              </Button>
            </div>
          ) : (
            "No contacts yet. Add one above."
          )}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs text-muted-foreground">
                <th className="px-3 py-2">
                  <input
                    type="checkbox"
                    aria-label="Select all contacts"
                    checked={allSelected}
                    onChange={() =>
                      setSelected(
                        allSelected
                          ? new Set()
                          : new Set(contacts.map((contact) => contact.id)),
                      )
                    }
                  />
                </th>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Phone</th>
                <th className="px-3 py-2 font-medium">Owner</th>
                <th className="px-3 py-2 font-medium">Team</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {contacts.map((contact) => {
                const ownerName = contact.owner_user_id
                  ? (memberById.get(contact.owner_user_id) ?? "Unknown")
                  : "Unassigned";
                const teamName = contact.department_id
                  ? (departmentById.get(contact.department_id) ?? "Unknown")
                  : "No team";
                return (
                  <tr key={contact.id}>
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        aria-label={`Select ${contact.display_name}`}
                        checked={selected.has(contact.id)}
                        onChange={() => toggleSelected(contact.id)}
                      />
                    </td>
                    <td className="px-3 py-2">{contact.display_name}</td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">
                      {contact.phones.length === 0 ? (
                        "—"
                      ) : (
                        <div className="flex flex-wrap items-center gap-1">
                          {contact.phones.map((entry, index) => (
                            <PhoneNumberMenu
                              key={`${entry.e164}-${index}`}
                              e164={entry.e164}
                              ariaLabel={`Actions for ${formatPhone(entry.e164)} (${contact.display_name})`}
                              onText={handleText}
                              // This page does not know which of our numbers to call
                              // from, so omit fromE164 and let the softphone pick the
                              // org default.
                              className="h-auto px-1 py-0 text-xs font-normal"
                            />
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">{ownerName}</td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">{teamName}</td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={!canAssign}
                        title={!canAssign ? "You don't have permission to reassign contacts." : undefined}
                        onClick={() => openAssignDrawer([contact])}
                      >
                        Assign
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {assigning && (
        <AssignOwnerDrawer
          contacts={assigning}
          onClose={() => {
            setAssigning(null);
            setSelected(new Set());
          }}
        />
      )}
    </div>
  );
}
