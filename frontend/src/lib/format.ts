/** Display helpers. Deliberately dumb: no locale guessing, no timezone maths. */

export function formatPhone(e164: string): string {
  const m = /^\+1(\d{3})(\d{3})(\d{4})$/.exec(e164);
  return m ? `(${m[1]}) ${m[2]}-${m[3]}` : e164;
}

/** Only digits and common phone-number punctuation - rejects free text with embedded
 * digits ("call 9725550199 now") before it ever reaches the digit-stripping below. */
const PHONE_CHARS_RE = /^[\d+().\-\s]+$/;

/** NANP (US/Canada) forbids an area code or exchange whose first digit is 0 or 1 - reject
 * those rather than silently normalizing an invalid domestic number. `tenDigits` is the
 * bare national number (area code + exchange + line), no country code. */
function hasValidNanpPrefixes(tenDigits: string): boolean {
  const areaCodeFirst = tenDigits[0];
  const exchangeFirst = tenDigits[3];
  return areaCodeFirst !== "0" && areaCodeFirst !== "1" && exchangeFirst !== "0" && exchangeFirst !== "1";
}

/** Best-effort raw-input -> E.164 normalization for the "New conversation" To field -
 * mirrors what a US-centric dial pad already assumes elsewhere (formatPhone above only
 * pretty-prints +1 numbers). A "+"-prefixed input is normalized by stripping punctuation
 * only (never assumed US/Canada, however it fails); a bare 10-digit or an 11-digit number
 * starting with "1" is assumed US/Canada, gated by hasValidNanpPrefixes above; anything
 * else is rejected rather than guessed at. Returns null when the input can't be
 * confidently normalized. */
export function normalizePhoneToE164(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed || !PHONE_CHARS_RE.test(trimmed)) return null;

  if (trimmed.startsWith("+")) {
    const candidate = `+${trimmed.slice(1).replace(/\D/g, "")}`;
    // A "+"-prefixed input is international-or-nothing - never fall through to the
    // NANP-assuming branches below on a bad international number.
    return /^\+[1-9]\d{6,14}$/.test(candidate) ? candidate : null;
  }

  const digits = trimmed.replace(/\D/g, "");
  if (digits.length === 10) {
    return hasValidNanpPrefixes(digits) ? `+1${digits}` : null;
  }
  if (digits.length === 11 && digits.startsWith("1")) {
    return hasValidNanpPrefixes(digits.slice(1)) ? `+${digits}` : null;
  }
  return null;
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
    case "pending":
      return { glyph: "…", label: "Pending", bad: false };
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

// ----------------------------------------------------------------------------------
// Item 51: SMS segment estimate, mirroring backend/app/providers/segments.py exactly -
// the same GSM 03.38 basic + extension tables, the same "one non-GSM char flips the
// WHOLE message to UCS-2" rule, and the same single/multi-segment thresholds. This is
// an estimate shown to the sender before sending; the carrier's own count (once a DLR
// arrives) is the billed truth and can differ.
// ----------------------------------------------------------------------------------
const GSM7_BASIC = new Set(
  "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?" +
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà",
);
const GSM7_EXTENDED = new Set("€[]{}|^~\\");
const GSM7_SINGLE = 160;
const GSM7_MULTI = 153;
const UCS2_SINGLE = 70;
const UCS2_MULTI = 67;

export interface SmsSegmentEstimate {
  encoding: "GSM-7" | "UCS-2";
  segments: number;
  units: number;
  /** Max units in the CURRENT segment count before another segment is needed. */
  limit: number;
}

function isGsm7(text: string): boolean {
  for (const ch of text) {
    if (!GSM7_BASIC.has(ch) && !GSM7_EXTENDED.has(ch)) return false;
  }
  return true;
}

export function estimateSmsSegments(text: string): SmsSegmentEstimate {
  if (!text) return { encoding: "GSM-7", segments: 1, units: 0, limit: GSM7_SINGLE };

  if (isGsm7(text)) {
    let units = 0;
    for (const ch of text) units += GSM7_EXTENDED.has(ch) ? 2 : 1;
    if (units <= GSM7_SINGLE) return { encoding: "GSM-7", segments: 1, units, limit: GSM7_SINGLE };
    const segments = Math.ceil(units / GSM7_MULTI);
    return { encoding: "GSM-7", segments, units, limit: segments * GSM7_MULTI };
  }

  // UCS-2 counts UTF-16 code units - a JS string's own .length already IS its UTF-16
  // code unit count (astral chars/most emoji are stored as a 2-unit surrogate pair and
  // counted as 2 automatically), so no extra iteration is needed here.
  const units = text.length;
  if (units <= UCS2_SINGLE) return { encoding: "UCS-2", segments: 1, units, limit: UCS2_SINGLE };
  const segments = Math.ceil(units / UCS2_MULTI);
  return { encoding: "UCS-2", segments, units, limit: segments * UCS2_MULTI };
}
