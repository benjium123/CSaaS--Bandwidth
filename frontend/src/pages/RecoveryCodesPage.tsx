import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import {
  AuthAlert,
  AuthButton,
  AuthPlate,
  AuthSurface,
} from "@/components/auth/AuthShell";

const RECOVERY_CODES_PATH = "/api/v1/auth/recovery-codes";


/**
 * The last thing a new owner sees: the one-time recovery codes that get them back in when they
 * lose their email. They are generated once by the API and shown exactly once - the screen says
 * so plainly, because there is no second chance to look at them.
 *
 * Nothing here is persisted by the client: the codes live in component state only, and the only
 * ways forward are copying them, downloading them, or explicitly ticking that they were saved.
 * Continue has no destination - it refresh `me` and leaves the wall in place, because only the
 * API decides that setup is finished.
 */
export function RecoveryCodesPage() {
  const { api, me, refreshMe, logout } = useAuth();
  const [codes, setCodes] = React.useState<string[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [saved, setSaved] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [copied, setCopied] = React.useState(false);

  // StrictMode mounts every effect twice; the codes are generated exactly once per screen.
  const started = React.useRef(false);
  const copyTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const generate = React.useCallback(async () => {
    if (started.current) return;
    started.current = true;
    setError(null);
    try {
      const res = await api.request<{ codes: string[] }>(RECOVERY_CODES_PATH, {
        method: "POST",
      });
      setCodes(res.codes);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [api]);

  React.useEffect(() => {
    void generate();
  }, [generate]);

  React.useEffect(
    () => () => {
      if (copyTimer.current !== null) clearTimeout(copyTimer.current);
    },
    [],
  );

  function retry() {
    started.current = false;
    void generate();
  }

  async function copyCodes() {
    if (!codes) return;
    setError(null);
    try {
      await navigator.clipboard.writeText(codes.join("\n"));
      setCopied(true);
      if (copyTimer.current !== null) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => {
        copyTimer.current = null;
        setCopied(false);
      }, 2000);
    } catch {
      setError("Copy failed - select the codes and copy them by hand.");
    }
  }

  function downloadCodes() {
    if (!codes) return;
    const body =
      `Ringlite recovery codes for ${me?.email ?? ""}\n` +
      `Generated ${new Date().toISOString().slice(0, 10)}\n` +
      `Each code works once.\n\n` +
      codes.join("\n") +
      "\n";
    const url = URL.createObjectURL(new Blob([body], { type: "text/plain" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "ringlite-recovery-codes.txt";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  async function handleContinue() {
    setBusy(true);
    try {
      await refreshMe();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const lede =
    "Store these somewhere safe, like a password manager. This is the only time we will show " +
    "them. Each code works once if you lose access to your email" +
    (me?.has_passkey || me?.totp_enabled ? ", passkey or authenticator app." : ".");

  return (
    <AuthSurface>
      <AuthPlate
        eyebrow="One more thing · Recovery codes"
        title="Save your recovery codes"
        lede={lede}
        footer={
          <button type="button" className="ex-link" onClick={logout}>
            Sign out
          </button>
        }
      >
        <div className="space-y-5">
          {error && <AuthAlert>{error}</AuthAlert>}

          {codes === null ? (
            error ? (
              <AuthButton type="button" tone="quiet" onClick={retry}>
                Try again
              </AuthButton>
            ) : (
              <p className="text-sm text-muted-foreground">Generating your codes...</p>
            )
          ) : (
            <>
              <ul
                aria-label="Recovery codes"
                className="grid grid-cols-2 gap-2 rounded-[3px] border border-border/70 p-3 font-mono text-sm"
              >
                {codes.map((code) => (
                  <li key={code}>{code}</li>
                ))}
              </ul>

              <div className="flex gap-2">
                <AuthButton type="button" tone="quiet" onClick={() => void copyCodes()}>
                  {copied ? "Copied" : "Copy codes"}
                </AuthButton>
                <AuthButton type="button" tone="quiet" onClick={downloadCodes}>
                  Download .txt
                </AuthButton>
              </div>

              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  aria-label="I have saved these codes"
                  checked={saved}
                  onChange={(e) => setSaved(e.target.checked)}
                />
                I have saved these codes somewhere safe
              </label>

              <AuthButton
                type="button"
                block
                disabled={!saved || codes.length === 0 || busy}
                onClick={() => void handleContinue()}
              >
                Continue
              </AuthButton>
            </>
          )}
        </div>
      </AuthPlate>
    </AuthSurface>
  );
}
