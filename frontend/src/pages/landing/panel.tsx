import clsx from "clsx";
import { MessageSquare, Phone, PhoneMissed, Voicemail } from "lucide-react";
import * as React from "react";

import "./panel.css";

/**
 * The shared chrome and the thread-entry pieces for the landing page's drawn surfaces.
 *
 * Everything here is a DRAWING of the product, not a source of information: the four
 * surface components in `surfaces.tsx` (round 2) put `aria-hidden="true"` on their own
 * root, and every claim these pieces appear to make is made in real prose elsewhere on
 * the page. Nothing in this file is interactive, and nothing here responds to hover.
 *
 * No `Math.random()` anywhere: the waveform heights are a fixed module-scope array so
 * the page paints identically on every render and every visit.
 */

/**
 * One-shot scroll reveal. Adds `is-in` to the element the first time it crosses the
 * threshold, then unobserves it - it must never re-hide on scroll up. When
 * `IntersectionObserver` is unavailable (jsdom in the test run), `is-in` is added
 * immediately so content is never left invisible.
 *
 * `is-in` means exactly one thing on this page: "this panel has been revealed". No
 * other component in this file may use it.
 */
export function useReveal(): React.RefObject<HTMLDivElement> {
  const ref = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;

    if (typeof IntersectionObserver === "undefined") {
      el.classList.add("is-in");
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-in");
            observer.unobserve(entry.target);
          }
        }
      },
      { threshold: 0.15, rootMargin: "0px 0px -10% 0px" },
    );

    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return ref;
}

export type PanelProps = {
  children: React.ReactNode;
  className?: string;
};

/**
 * The shared frame: 1px rule, 4px radius, a charge filament across the top edge. The
 * four surfaces in round 2 wrap their own content in this and put `aria-hidden` on
 * their own root - `Panel` deliberately does not, so the surfaces stay in control of
 * what is announced.
 */
export function Panel({ children, className }: PanelProps): React.JSX.Element {
  const reveal = useReveal();
  return (
    <div ref={reveal} className={clsx("lps-panel lp-reveal", className)}>
      <span className="lps-filament" />
      {children}
    </div>
  );
}

/** A centred mono date on a copper hairline rule. */
export function DayDivider({ label }: { label: string }): React.JSX.Element {
  return (
    <div className="lps-day">
      <span className="lps-day-rule" />
      <span className="ex-label lps-day-label">{label}</span>
      <span className="lps-day-rule" />
    </div>
  );
}

export type CallEntryProps = {
  name: string;
  meta: string;
  time: string;
  missed?: boolean;
};

/**
 * An inbound call, or a missed one - the glyph carries the difference. A missed call is
 * an ordinary event, not a system fault, so it gets the neutral lamp; `--ex-danger` is
 * reserved for faults and is not spent here.
 */
export function CallEntry({
  name,
  meta,
  time,
  missed = false,
}: CallEntryProps): React.JSX.Element {
  const Glyph = missed ? PhoneMissed : Phone;
  return (
    <div className={clsx("lps-entry", missed && "is-missed")}>
      <span className="lps-glyph">
        <Glyph aria-hidden="true" size={13} />
      </span>
      <div className="lps-entry-body">
        <div className="lps-entry-head">
          <span className="lps-name">{name}</span>
          <span className="ex-mono lps-time">{time}</span>
        </div>
        <div className="lps-entry-meta">
          <span className={clsx("ex-lamp", !missed && "ex-lamp-live")} />
          <span className="ex-mono lps-meta">{meta}</span>
        </div>
      </div>
    </div>
  );
}

/**
 * The waveform heights. Fixed at module scope on purpose: a `Math.random()` in the
 * component body would re-roll on every render, and the page must paint identically
 * every time. 40 values, each between 15 and 100.
 */
const WAVE_HEIGHTS: readonly number[] = [
  22, 48, 71, 34, 88, 56, 19, 63, 92, 41, 27, 74, 58, 31, 85, 46, 68, 24, 97, 52, 37, 79,
  44, 61, 29, 90, 55, 18, 66, 83, 39, 72, 26, 94, 49, 33, 77, 59, 21, 87,
];

/** The scrub position: bars before this index are played, the rest are not. */
const SCRUB = 23;

export type VoicemailEntryProps = {
  name: string;
  length: string;
  time: string;
};

/** A missed call that left a voicemail, drawn as a CSS waveform. */
export function VoicemailEntry({
  name,
  length,
  time,
}: VoicemailEntryProps): React.JSX.Element {
  return (
    <div className="lps-entry is-missed">
      <span className="lps-glyph">
        <Voicemail aria-hidden="true" size={13} />
      </span>
      <div className="lps-entry-body">
        <div className="lps-entry-head">
          <span className="lps-name">{name}</span>
          <span className="ex-mono lps-time">{time}</span>
        </div>
        <div className="lps-entry-meta">
          <span className="ex-lamp ex-lamp-wait" />
          <span className="ex-mono lps-meta">{length}</span>
        </div>
        <div className="lps-wave">
          {WAVE_HEIGHTS.map((h, i) => (
            <span
              key={i}
              className={clsx("lps-wave-bar", i < SCRUB && "is-played")}
              style={{ height: `${h}%` }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

export type TextEntryProps = {
  body: string;
  time: string;
  outbound: boolean;
  state?: string;
};

/**
 * A text message. Outbound sits right on ink-high; inbound sits left on ink-raise. The
 * direction modifiers are `is-outbound` / `is-inbound` - `is-in` is reserved for the
 * panel reveal and must not be reused here.
 */
export function TextEntry({
  body,
  time,
  outbound,
  state,
}: TextEntryProps): React.JSX.Element {
  return (
    <div className={clsx("lps-entry", outbound ? "is-outbound" : "is-inbound")}>
      <span className="lps-glyph">
        <MessageSquare aria-hidden="true" size={13} />
      </span>
      <div className="lps-entry-body">
        <div className={clsx("lps-bubble", outbound ? "is-outbound" : "is-inbound")}>
          <span className="lps-bubble-body">{body}</span>
        </div>
        <div className="lps-entry-meta">
          <span className="ex-mono lps-time">{time}</span>
          {state ? <span className="ex-mono lps-state">{state}</span> : null}
        </div>
      </div>
    </div>
  );
}

/*
 * lps-* class names used in this file, for round 3's surfaces.css:
 *
 *   lps-panel        the shared frame (border, radius, gradient, shadow)
 *   lps-filament     the charge filament across the top edge
 *   lps-day          the day-divider row
 *   lps-day-rule     the copper hairline either side of the date
 *   lps-day-label    the centred mono date
 *   lps-entry        one row in the thread
 *   lps-entry-body   the column to the right of the glyph
 *   lps-entry-head   name + timestamp row
 *   lps-entry-meta   lamp + meta row, or time + state row
 *   lps-glyph        the left gutter holding the channel icon
 *   lps-name         the contact name
 *   lps-time         the timestamp (mono)
 *   lps-meta         the duration / length (mono)
 *   lps-state        the delivery state under an outbound bubble (mono)
 *   lps-wave         the waveform container
 *   lps-wave-bar     one bar; `.is-played` marks the bars before the scrub
 *   lps-bubble       the message bubble
 *   lps-bubble-body  the message text inside the bubble
 *
 * Modifier classes: `.is-missed`, `.is-outbound`, `.is-inbound`, `.is-played`.
 * `.is-in` is NOT a modifier here - it is the reveal state added by `useReveal` to
 * `.lp-reveal`, and it means exactly one thing on this page.
 * Reused from authTheme.css: `.ex-label`, `.ex-mono`, `.ex-lamp`, `.ex-lamp-live`,
 * `.ex-lamp-wait`, `.ex-lamp-fault`.
 */
