import type { ReactNode } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { CtaBand, SitePage } from "@/marketing/SiteChrome";
import { POSTS, PUBLISHED_POSTS, postBySlug, type Block, IS_PROD_BUILD } from "@/marketing/blog/posts";

/** "2026-10-03" -> "October 3, 2026". */
function formatDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}

/**
 * Turn block text into nodes. `[label](/path)` becomes an internal link; anything else,
 * including a bracket pair that does not point at a path on this site, stays as text.
 */
function inline(text: string): ReactNode[] {
  const pattern = /\[([^\]]+)\]\((\/[^)\s]*)\)/g;
  const nodes: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    nodes.push(
      <Link key={`${match.index}-${match[2]}`} to={match[2]}>
        {match[1]}
      </Link>,
    );
    last = match.index + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function renderBlock(block: Block, key: number): ReactNode {
  switch (block.kind) {
    case "p":
      return <p key={key}>{inline(block.text)}</p>;
    case "h2":
      return <h2 key={key}>{block.text}</h2>;
    case "list":
      return (
        <ul className="ms-textblock-list" key={key}>
          {block.items.map((item, i) => (
            <li key={i}>{inline(item)}</li>
          ))}
        </ul>
      );
    case "table":
      return (
        <div className="ms-matrix-section" key={key}>
          <table className="ms-matrix-table">
            {block.caption && <caption>{block.caption}</caption>}
            <thead>
              <tr>
                {block.head.map((cell, i) => (
                  <th key={i} scope="col">
                    {cell}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, i) => (
                <tr key={i}>
                  {row.map((cell, j) => (
                    <td key={j}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "cta":
      return (
        <p className="ms-cta-row" key={key}>
          <Link className="rl-button" to={block.to}>
            {block.label}
          </Link>{" "}
          <span>{block.text}</span>
        </p>
      );
    default:
      return null;
  }
}

/** /blog/:slug — one guide, its meta line, the body and a small "More guides" list. */
export function BlogPostPage() {
  const { slug = "" } = useParams();
  const post = postBySlug(slug);

  if (!post) return <Navigate to="/blog" replace />;
  if (IS_PROD_BUILD && post.draft) return <Navigate to="/blog" replace />;

  const visible = IS_PROD_BUILD ? PUBLISHED_POSTS : POSTS;
  const more = visible.filter(other => other.slug !== post.slug).slice(0, 2);

  return (
    <SitePage title={post.title} description={post.description}>
      <section className="ms-page-hero rl-wrap">
        <p className="rl-eyebrow"><span />{post.topic}</p>
        <h1 className="ms-h1">{post.title}</h1>
        <p className="rl-mono">
          Ringlite team \u00b7 {formatDate(post.published)} \u00b7 {post.readMinutes} min read
        </p>
      </section>

      <article className="ms-article rl-wrap">{post.blocks.map((block, i) => renderBlock(block, i))}</article>

      {more.length > 0 && (
        <section className="ms-related rl-wrap rl-reveal" aria-label="More guides">
          <h2>More guides</h2>
          {more.map(other => (
            <Link to={`/blog/${other.slug}`} key={other.slug}>
              <p className="rl-mono">{other.topic}</p>
              <h3>{other.title}</h3>
              <p>{other.description}</p>
            </Link>
          ))}
        </section>
      )}

      <CtaBand title="One price for your team and your numbers." />
    </SitePage>
  );
}
