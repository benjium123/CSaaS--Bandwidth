/** Display helpers. Deliberately dumb: no locale guessing, no timezone maths. */

export function formatPhone(e164: string): string {
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(e164);
  return m ? `(${m[1]}) ${m[2]}-${m[3]}` : e164;
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.floor((Date.now() - then) / 1000);
  if (secs < 60) return "now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  if (secs < 604800) return `${Math.floor(secs / 86400)}d`;
  return new Date(iso).toLocaleDateString();
}

/** Delivery ticks, driven purely by polled status. */
export function statusTick(status: string): { glyph: string; label: string; bad: boolean } {
  switch (status) {
    case "queued":
    case "accepted":
      return { glyph: "○", label: "Queued", bad: false };
    case "sending":
      return { glyph: "◔", label: "Sending", bad: false };
    case "delivered":
      return { glyph: "✓", label: "Delivered", bad: false };
    case "failed":
    case "rejected":
      return { glyph: "✗", label: "Failed", bad: true };
    case "received":
      return { glyph: "", label: "Received", bad: false };
    default:
      return { glyph: "·", label: status, bad: false };
  }
}
