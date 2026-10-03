import { Link } from "react-router-dom";
import { SitePage } from "@/marketing/SiteChrome";
import { POSTS, PUBLISHED_POSTS, IS_PROD_BUILD } from "@/marketing/blog/posts";

/**
 * /blog — the list of guides. In a production build only published posts appear; everywhere
 * else the drafts are listed too and labelled so they are easy to spot while the site is
 * being worked on.
 */
export function BlogIndexPage() {
  const posts = IS_PROD_BUILD ? PUBLISHED_POSTS : POSTS;

  return (
    <SitePage
      title="Business phone and texting guides"
      description="Plain guides on business phone costs, 10DLC texting registration and switching providers, from the Ringlite team."
    >
      <section className="ms-page-hero rl-wrap">
        <p className="rl-eyebrow"><span />GUIDES</p>
        <h1 className="ms-h1">Guides for small teams on the phone</h1>
        <p className="ms-lede">
          Plain write-ups on what business phone service costs, how 10DLC texting registration
          works, and what switching providers takes. Written for teams that do not have someone
          whose whole job is phone systems.
        </p>
      </section>

      {posts.length === 0 ? (
        <section className="ms-textblock rl-wrap rl-reveal">
          <p>The first guides are on their way.</p>
        </section>
      ) : (
        <section className="ms-related rl-wrap rl-reveal" aria-label="Guides">
          {posts.map(post => (
            <Link to={`/blog/${post.slug}`} key={post.slug}>
              <p className="rl-mono">{post.draft ? `${post.topic} \u00b7 Draft` : post.topic}</p>
              <h2>{post.title}</h2>
              <p>{post.description}</p>
              <p className="rl-mono">{post.readMinutes} min read</p>
            </Link>
          ))}
        </section>
      )}
    </SitePage>
  );
}
