import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "@/App";
import { POSTS, PUBLISHED_POSTS, type Block } from "@/marketing/blog/posts";
import { PUBLIC_ROUTES } from "@/prerender/routes";
import { buildRss } from "@/prerender/seo";

const auth = vi.hoisted(() => ({ ready: false, me: null as any, orgId: null }));
vi.mock("@/auth/AuthContext", () => ({ useAuth: () => auth }));
vi.mock("@/pages/SignUpPage", () => ({ SignUpPage: () => <h1>Signup destination</h1> }));

beforeEach(() => {
  auth.ready = false;
  auth.me = null;
  localStorage.clear();
});

function open(path: string) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Every string a block will put on the page, links and all. */
function blockStrings(block: Block): string[] {
  switch (block.kind) {
    case "p":
    case "h2":
      return [block.text];
    case "list":
      return block.items;
    case "table":
      return [...block.head, ...block.rows.flat(), block.caption ?? ""];
    case "cta":
      return [block.text, block.label];
    default:
      return [];
  }
}

/** The four per-competitor cost guides added on top of the base posts. */
const COMPARISON_POSTS = POSTS.filter(post => post.slug.endsWith("-vs-ringlite-cost") && post.slug !== "quo-vs-ringlite-cost");

describe("Blog", () => {
  it("renders one h1 on /blog and links every post", () => {
    open("/blog");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(POSTS).toHaveLength(9);
    for (const post of POSTS) {
      expect(screen.getByText(post.title).closest("a")).toHaveAttribute("href", `/blog/${post.slug}`);
    }
  });

  it.each(POSTS.map(post => [post.slug, post.title] as const))(
    "renders /blog/%s with its own h1 and at least one h2",
    (slug, title) => {
      const { unmount } = open(`/blog/${slug}`);
      const h1s = screen.getAllByRole("heading", { level: 1 });
      expect(h1s).toHaveLength(1);
      expect(h1s[0]).toHaveTextContent(title);
      expect(screen.getAllByRole("heading", { level: 2 }).length).toBeGreaterThan(0);
      unmount();
    },
  );

  it("sends an unknown slug back to the index", () => {
    open("/blog/nope");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Guides for small teams on the phone");
  });

  it("keeps post metadata sane", () => {
    for (const post of POSTS) {
      expect(post.description.length).toBeGreaterThanOrEqual(120);
      expect(post.description.length).toBeLessThanOrEqual(170);
      expect(post.slug).toMatch(/^[a-z0-9-]+$/);
      for (const block of post.blocks) {
        for (const text of blockStrings(block)) {
          expect(text).not.toContain("TODO");
        }
      }
    }
  });

  it("builds an RSS feed with one item per post", () => {
    const xml = buildRss(POSTS);
    expect(xml.startsWith("<?xml")).toBe(true);
    expect(xml.match(/<item>/g) ?? []).toHaveLength(POSTS.length);
  });

  it("adds published posts to the sitemap", () => {
    expect(COMPARISON_POSTS).toHaveLength(5);
    expect(PUBLISHED_POSTS).toHaveLength(POSTS.length);
    expect(COMPARISON_POSTS.every(post => !post.draft)).toBe(true);
    expect(PUBLIC_ROUTES.some(route => route.path.startsWith("/blog"))).toBe(true);
  });

  it("gives every comparison post a four-row cost table and no broken values", () => {
    for (const post of COMPARISON_POSTS) {
      const costTable = post.blocks.find(block => block.kind === "table" && block.rows.length === 4);
      expect(costTable).toBeDefined();
      for (const block of post.blocks) {
        for (const text of blockStrings(block)) {
          expect(text).not.toContain("NaN");
          expect(text).not.toContain("undefined");
        }
      }
    }
  });

  it("states where Ringlite and KrispCall are each cheaper", () => {
    const post = POSTS.find(item => item.slug === "krispcall-vs-ringlite-cost");
    expect(post).toBeDefined();
    const para = post?.blocks.find(block => block.kind === "p" && block.text.includes("costs the same or less"));
    expect(para).toBeDefined();
    const text = para && para.kind === "p" ? para.text : "";
    expect(text).toContain("KrispCall costs the same or less at 3 and 5 people");
    expect(text).toContain("Ringlite costs less at 10 and 15 people");
  });
});
