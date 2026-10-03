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

describe("Blog", () => {
  it("renders one h1 on /blog and links every draft post", () => {
    open("/blog");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(POSTS).toHaveLength(4);
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

  it("leaves blog paths out of the sitemap while every post is a draft", () => {
    expect(PUBLISHED_POSTS).toHaveLength(0);
    expect(POSTS.every(post => post.draft)).toBe(true);
    expect(PUBLIC_ROUTES.some(route => route.path.startsWith("/blog"))).toBe(false);
  });
});
