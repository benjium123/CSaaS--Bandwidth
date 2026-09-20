import * as React from "react";
import { QRCodeSVG } from "qrcode.react";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

/**
 * Shared TOTP enrolment body, used by both the forced wall (SecureAccountPage) and the
 * settings page (SettingsSecurityPage).
 *
 * The QR is generated LOCALLY in the browser by qrcode.react: the provisioning URI is
 * handed straight to <QRCodeSVG value=... />, which emits only SVG path geometry. Nothing
 * here ever builds a URL carrying the secret or the URI, so there is no request to an
 * external QR image service and no credential leaked into a DOM attribute.
 *
 * COLOUR TOKENS: this component mounts BOTH inside `.console-surface` (the settings page)
 * and outside it (the auth-shell wall), and the `--cx-*` colour tokens are declared only on
 * `.console-surface`. So it uses the global `--background`/`--foreground`/`--muted`/
 * `--muted-foreground`/`--border`/`--destructive` family from index.css, which resolves in
 * both scopes. The RADIUS tokens live on `:root`, so the two-argument `var(--cx-r-*, ...)`
 * form is safe everywhere.
 */
function groupKey(secret: string): string {
  const chunks = secret.match(/.{1,4}/g);
  return chunks ? chunks.join(" ") : "";
}

export function TotpEnrolment({
  secret,
  uri,
  className,
}: {
  secret: string;
  uri: string;
  className?: string;
}): React.ReactElement {
  const [status, setStatus] = React.useState<"idle" | "copied" | "failed">("idle");
  const resetTimer = React.useRef<number | null>(null);

  // Clearing the timer on unmount is what keeps a late setState from firing after the
  // page has moved on (e.g. activation succeeds and the enrol block disappears).
  React.useEffect(() => {
    return () => {
      if (resetTimer.current !== null) {
        window.clearTimeout(resetTimer.current);
        resetTimer.current = null;
      }
    };
  }, []);

  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("unavailable");
      // The EXACT raw secret - the 4-character grouping below is display-only.
      await navigator.clipboard.writeText(secret);
      setStatus("copied");
      if (resetTimer.current !== null) window.clearTimeout(resetTimer.current);
      resetTimer.current = window.setTimeout(() => {
        resetTimer.current = null;
        setStatus("idle");
      }, 2000);
    } catch {
      // The key stays select-all, so the user can still copy it by hand. Never surface as
      // "Copied": a failed write must not look like a success.
      setStatus("failed");
    }
  }

  return (
    <div className={cn("space-y-[12px]", className)}>
      {/* Global --*-foreground family, not --cx-subtle: this mounts outside .console-surface too. */}
      <p className="text-[13px] leading-relaxed text-muted-foreground">
        Scan this QR code with your authenticator app, or enter the key by hand.
      </p>

      {/*
        Deliberate exception to theming: this is the ONE element that ignores dark mode. A
        QR code is read by a camera, so it needs genuinely dark modules on a genuinely
        light background plus the spec's 4-module quiet zone. Inverting it for dark mode
        would make it unscannable - hence the literal `bg-white` and hex fgColour below,
        and a fixed `border-black/10` edge rather than a theme token (a theme token would
        give the light panel a border in the page's text colour in one of the two scopes).
      */}
      <div className="inline-flex rounded-[var(--cx-r-md,14px)] border border-black/10 bg-white p-[12px]">
        <QRCodeSVG
          value={uri}
          size={176}
          level="M"
          bgColor="#ffffff"
          fgColor="#0b0f14"
          marginSize={4}
          title="Two-factor setup QR code"
          data-testid="totp-qr"
          role="img"
          aria-label="Two-factor setup QR code"
        />
      </div>

      <div className="space-y-[8px]">
        <p className="text-[13px] text-muted-foreground">Or enter this key manually</p>
        {/* Global --border/--muted/--foreground family, not --cx-*: shared across two token scopes. */}
        <code
          data-testid="totp-secret"
          data-secret={secret}
          className="select-all block break-all rounded-[var(--cx-r-sm,12px)] border border-border bg-muted p-[11px] font-mono text-[12px] tracking-[0.08em] text-foreground"
        >
          {groupKey(secret)}
        </code>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="rounded-full"
          onClick={copy}
        >
          {status === "copied" ? "Copied" : "Copy key"}
        </Button>
        {status === "failed" && (
          <p role="status" className="text-[13px] text-destructive">
            {"Couldn't copy automatically - select the key and copy it."}
          </p>
        )}
      </div>
    </div>
  );
}
