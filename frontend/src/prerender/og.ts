/** Social preview images live at /og/<file>.png; scripts/og-images.ts renders them. */
export const OG_DIR = "og";
export const OG_DEFAULT = "default.png";

/** Which preview image a public path uses. Alternatives share their compare page's card. */
export function ogFileFor(path: string): string {
  if (path === "/") return "home.png";
  if (path === "/pricing") return "pricing.png";
  if (path === "/calculator") return "calculator.png";
  if (path === "/blog") return "blog.png";
  const vs = /^\/(?:compare|alternatives)\/([a-z0-9-]+)$/.exec(path);
  if (vs) return `compare-${vs[1]}.png`;
  const post = /^\/blog\/([a-z0-9-]+)$/.exec(path);
  if (post) return `blog-${post[1]}.png`;
  return OG_DEFAULT;
}
