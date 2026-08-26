import { useAuth } from "@/auth/AuthContext";
import { Button } from "@/components/ui/primitives";

export function OrgPickerPage() {
  const { me, selectOrg, logout } = useAuth();
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-sm space-y-4 rounded-lg border border-border p-6">
        <h1 className="text-lg font-semibold">Choose an organization</h1>
        {me?.memberships.length ? (
          <ul className="space-y-2">
            {me.memberships.map((m) => (
              <li key={m.org_id}>
                <Button
                  variant="outline"
                  className="w-full justify-between"
                  onClick={() => selectOrg(m.org_id)}
                >
                  <span>{m.org_name}</span>
                  <span className="text-xs text-muted-foreground">{m.role_name}</span>
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">
            You are not a member of any organization yet.
          </p>
        )}
        <Button variant="ghost" className="w-full" onClick={logout}>
          Sign out
        </Button>
      </div>
    </div>
  );
}
