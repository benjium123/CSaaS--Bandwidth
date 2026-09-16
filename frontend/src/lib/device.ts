/**
 * P41: a random per-browser id sent as X-Device-Id so the backend can tell a known device
 * from a new one at sign-in. It identifies nothing about the person - the server stores
 * only its SHA-256 - and clearing site data simply makes this browser "new" again.
 */

const DEVICE_KEY = "csaas.device";

function randomId(): string {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function deviceId(): string {
  try {
    const existing = localStorage.getItem(DEVICE_KEY);
    if (existing && existing.length >= 16) return existing;
    const next = randomId();
    localStorage.setItem(DEVICE_KEY, next);
    return next;
  } catch {
    return randomId();
  }
}
