export type DesktopNotice = { title: string; body: string; tag?: string; route?: string };

export interface RingliteDesktop {
  version: string;
  platform: string;
  notify(notice: DesktopNotice): void;
  setBadge(count: number): void;
}

declare global {
  interface Window {
    ringliteDesktop?: RingliteDesktop;
  }
}

/**
 * The Electron preload script exposes `window.ringliteDesktop`. In a normal browser (and in
 * jsdom-based tests without an explicit stub) it is undefined, so the console must degrade to a
 * no-op. We also reject foreign / partially-implemented objects: a shell that only exposes
 * `version` would otherwise crash every notify() call site.
 *
 * @returns the bridge, or null when running in a plain browser.
 */
export function getDesktopBridge(): RingliteDesktop | null {
  if (typeof window === "undefined") return null;
  const bridge = window.ringliteDesktop;
  if (!bridge || typeof bridge !== "object") return null;
  if (typeof bridge.notify !== "function") return null;
  if (typeof bridge.setBadge !== "function") return null;
  return bridge;
}
