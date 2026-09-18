import { Link } from "react-router-dom";
import { useAuth } from "@/auth/AuthContext";
import { AuthPlate, AuthSurface, Lamp } from "@/components/auth/AuthShell";

export function OrgPickerPage() {
  const { me, selectOrg, logout } = useAuth();
  return (
    <AuthSurface>
      <AuthPlate
        eyebrow="Signed in · Choose a workspace"
        title="Choose an organization"
        // Was "You belong to more than one." - which this page cheerfully said to an
        // account that belongs to NONE. Found by signing in as a real membership-less
        // account against a real backend, which is the sort of thing only running it
        // catches. The lede now describes what the page DOES; how many workspaces the
        // person has is the list's business, and the list waits until it knows.
        lede="Everything you see next is scoped to the workspace you pick."
        footer={
          <button type="button" className="ex-link" onClick={logout}>
            Sign out
          </button>
        }
      >
        {/* `me` is null until /auth/me answers, so this says nothing at all until it has -
         * an empty list must never be rendered as "you are not a member of anything" while
         * the answer is still in flight. */}
        {me == null ? (
          // A statement about the REQUEST, never about the account: "we are still asking"
          // is something the client knows; "you have no workspaces" is not, until the
          // answer arrives. Saying nothing at all would leave an unexplained gap on a slow
          // or failed /auth/me.
          <Lamp state="wait">Checking your workspaces…</Lamp>
        ) : me.memberships.length ? (
          <ul className="space-y-2">
            {me.memberships.map((m) => (
              <li key={m.org_id}>
                <button
                  type="button"
                  className="ex-btn ex-btn-quiet w-full justify-between"
                  onClick={() => selectOrg(m.org_id)}
                >
                  <span className="truncate">{m.org_name}</span>
                  <span className="ex-label">{m.role_name}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">
            You are not a member of any organization yet.
          </p>
        )}

        {me?.is_platform_operator ? (
          <>
            <hr className="ex-hairline my-5" />
            <Link to="/ops" className="ex-btn ex-btn-quiet w-full">
              Open the operator console
            </Link>
          </>
        ) : null}
      </AuthPlate>
    </AuthSurface>
  );
}
