import { useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Button } from "@/components/ui/primitives";

/** P42: owners, admins, billing staff and operators must sign in with a passkey. Shown until
 * they have one; after the grace date the API refuses admin features without it. */
export function PasskeyGraceBanner() {
  const { me } = useAuth();
  const navigate = useNavigate();
  if (!me?.passkey_required || me.has_passkey) return null;
  const until = me.passkey_grace_until ? new Date(me.passkey_grace_until) : null;
  const expired = until !== null && until.getTime() < Date.now();

  return (
    <div role="status" aria-label="Passkey required" className="border-b border-border bg-muted px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="text-sm font-semibold">
            {expired ? "Add a passkey to use admin features" : "Add a passkey to your account"}
          </p>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Accounts with admin or billing access sign in with a passkey - it cannot be phished.
            {until && !expired ? ` Required from ${until.toLocaleDateString()}.` : ""}
          </p>
        </div>
        <Button type="button" onClick={() => navigate("/settings/team?tab=security")}>
          Add a passkey
        </Button>
      </div>
    </div>
  );
}
