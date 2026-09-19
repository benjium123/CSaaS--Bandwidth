import { useNavigate } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { Button } from "@/components/ui/primitives";
import { BANNER_PRIORITY, BannerSlot } from "@/components/shell/BannerSlot";
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
    <BannerSlot priority={BANNER_PRIORITY.passkey}>
      {/* `.ex-strip` (src/auth/authTheme.css, NOT ours) is a DELIBERATELY ALWAYS-DARK strip:
          it hardcodes its own #1A1D24 background and pins --ex-copper/--ex-verdigris so the
          saturated hues cannot follow the theme. It does NOT pin a dim-text token, so the
          console's --cx-subtle (which --ex-bone-dim aliases to inside `.console-surface`)
          flips to a DARK grey in light mode and would land at roughly 2.2:1 on this strip.
          The dim copy below is therefore white-at-opacity — theme-invariant, like the
          surface it sits on, and matching the `text-white` it shares a line with. It is not
          a palette literal and it is not --cx-subtle, for that reason. */}
      <div role="status" aria-label="Passkey required" className="ex-strip px-4 py-[9px]">
        <div className="flex items-center gap-[11px]">
          <div className="ex-label flex shrink-0 items-center gap-2">
            <span className={`ex-lamp ${expired ? "ex-lamp-fault" : "ex-lamp-wait"}`} />
            {expired ? "Passkey required" : "Passkey required soon"}
          </div>
          <p className="min-w-0 flex-1 truncate text-[13px]">
            <span className="font-semibold text-white">
              {expired ? "Add a passkey to use admin features" : "Add a passkey to your account"}
            </span>
            <span aria-hidden="true" className="text-white/55"> — </span>
            <span className="text-white/70">
              Accounts with admin or billing access sign in with a passkey - it cannot be phished.
              {until && !expired ? ` Required from ${until.toLocaleDateString()}.` : ""}
            </span>
          </p>
          <Button
            type="button"
            size="sm"
            className="shrink-0"
            onClick={() => navigate("/settings/team?tab=security")}
          >
            Add a passkey
          </Button>
        </div>
      </div>
    </BannerSlot>
  );
}
