import { useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Button } from "@/components/ui/primitives";
import "@/auth/authTheme.css";

/** P42: owners, admins, billing staff and operators must sign in with a passkey. Shown until
 * they have one; after the grace date the API refuses admin features without it.
 *
 * The guard is `!me?.passkey_required` returning null, which is correct here for the
 * unusual reason that the null and false cases WANT the same outcome: while `me` is
 * loading we know nothing, and a banner asserting a security requirement is exactly the
 * kind of claim that must wait for the server. Silence is the safe default for a warning;
 * it is not the safe default for a reassurance, which is why this component has none. */
export function PasskeyGraceBanner() {
  const { me } = useAuth();
  const navigate = useNavigate();
  if (!me?.passkey_required || me.has_passkey) return null;
  const until = me.passkey_grace_until ? new Date(me.passkey_grace_until) : null;
  const expired = until !== null && until.getTime() < Date.now();

  return (
    <div role="status" aria-label="Passkey required" className="ex-strip px-4 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="ex-label mb-1.5 flex items-center gap-2">
            <span className={`ex-lamp ${expired ? "ex-lamp-fault" : "ex-lamp-wait"}`} />
            {expired ? "Passkey required" : "Passkey required soon"}
          </div>
          <p className="text-sm font-semibold text-white">
            {expired ? "Add a passkey to use admin features" : "Add a passkey to your account"}
          </p>
          <p className="mt-0.5 text-sm text-neutral-400">
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
