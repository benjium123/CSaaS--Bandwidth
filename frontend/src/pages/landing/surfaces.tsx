import clsx from "clsx";
import * as React from "react";
import "./surfaces.css";
import {
  CallEntry,
  DayDivider,
  Panel,
  TextEntry,
  VoicemailEntry,
  useReveal,
} from "./panel";

/**
 * The four drawn product surfaces.
 *
 * Each is a DRAWING of the product, not a source of information: every root carries
 * `aria-hidden="true"`, and every claim they appear to make is made in real prose
 * elsewhere on the page. Nothing here is interactive and nothing responds to hover.
 *
 * The chrome (frame, filament, reveal) lives in `panel.tsx`; the timeline rows are the
 * entry components from the same file, so S1 and S2 cannot drift apart.
 */

export { Panel, useReveal };

/* ------------------------------------------------------------------ S1 Console */

const CONSOLE_LINES: readonly { frag: string; active?: boolean }[] = [
  { frag: "415 ••• 0142", active: true },
  { frag: "415 ••• 0188" },
  { frag: "212 ••• 7731" },
  { frag: "44 20 •••• 3310" },
];

const CONSOLE_THREADS: readonly {
  name: string;
  preview: string;
  time: string;
  lamp: "live" | "wait" | "idle";
  selected?: boolean;
}[] = [
  {
    name: "Dana Whitfield",
    preview: "Thursday at 10 works — I'll send a calendar invite.",
    time: "09:12",
    lamp: "live",
    selected: true,
  },
  {
    name: "Marcus Webb",
    preview: "Can you do Thursday morning instead?",
    time: "08:47",
    lamp: "wait",
  },
  {
    name: "Priya Raman",
    preview: "Left a voicemail about the site visit.",
    time: "08:20",
    lamp: "idle",
  },
  {
    name: "Jonah Feld",
    preview: "Thanks — that answers it.",
    time: "Yest",
    lamp: "idle",
  },
  {
    name: "Ruth Okafor",
    preview: "Ringing back after lunch.",
    time: "Yest",
    lamp: "idle",
  },
];

export function Console(): React.JSX.Element {
  return (
    <div className="lps-console" aria-hidden="true">
      <Panel className="lps-console-panel">
        <div className="lps-console-grid">
          <aside className="lps-rail">
            <div className="lps-rail-mark">
              <span className="ex-nameplate lps-rail-name">CSaaS</span>
              <span className="ex-label">Exchange</span>
            </div>
            <div className="lps-rail-lines">
              {CONSOLE_LINES.map((l) => (
                <div
                  key={l.frag}
                  className={clsx("lps-rail-line", l.active && "is-active")}
                >
                  <span
                    className={clsx("ex-lamp", l.active && "ex-lamp-live")}
                  />
                  <span className="ex-mono lps-rail-frag">{l.frag}</span>
                </div>
              ))}
            </div>
          </aside>

          <div className="lps-threadlist">
            <div className="lps-threadlist-head">
              <span className="ex-label">All conversations</span>
            </div>
            {CONSOLE_THREADS.map((t) => (
              <div
                key={t.name}
                className={clsx("lps-threadrow", t.selected && "is-selected")}
              >
                <span
                  className={clsx(
                    "ex-lamp",
                    t.lamp === "live" && "ex-lamp-live",
                    t.lamp === "wait" && "ex-lamp-wait",
                  )}
                />
                <span className="lps-threadrow-body">
                  <span className="lps-threadrow-name">{t.name}</span>
                  <span className="lps-threadrow-preview">{t.preview}</span>
                </span>
                <span className="ex-mono lps-threadrow-time">{t.time}</span>
              </div>
            ))}
          </div>

          <div className="lps-open">
            <div className="lps-open-head">
              <span className="lps-open-name">Dana Whitfield</span>
              <span className="ex-mono lps-open-number">415 ••• 0142</span>
            </div>
            <div className="lps-open-timeline">
              <CallEntry
                name="Dana Whitfield"
                meta="Inbound · 4m 12s"
                time="09:04"
              />
              <TextEntry
                body="Morning — could we move Thursday to 10?"
                time="09:06"
                outbound={false}
              />
              <TextEntry
                body="Thursday at 10 works — I'll send a calendar invite."
                time="09:12"
                outbound
                state="delivered"
              />
            </div>
          </div>
        </div>
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------------------- S2 Thread */

export function Thread(): React.JSX.Element {
  return (
    <div className="lps-thread" aria-hidden="true">
      <Panel className="lps-thread-panel">
        <div className="lps-thread-head">
          <span className="lps-thread-name">Dana Whitfield</span>
          <span className="ex-mono lps-thread-number">415 ••• 0142</span>
        </div>
        <div className="lps-thread-timeline">
          <DayDivider label="Mon 14 Apr" />
          <CallEntry
            name="Dana Whitfield"
            meta="Inbound · 4m 12s"
            time="09:04"
          />
          <TextEntry
            body="Morning — could we move Thursday to 10?"
            time="09:06"
            outbound={false}
          />
          <TextEntry
            body="Thursday at 10 works — I'll send a calendar invite."
            time="09:12"
            outbound
            state="delivered"
          />
          <DayDivider label="Tue 15 Apr" />
          <CallEntry
            name="Dana Whitfield"
            meta="Missed · rang twice"
            time="08:41"
            missed
          />
          <VoicemailEntry
            name="Dana Whitfield"
            length="0:38"
            time="08:42"
          />
          <TextEntry
            body="Sorry to miss you — I'll try again after lunch."
            time="08:44"
            outbound={false}
          />
        </div>
      </Panel>
    </div>
  );
}

/* -------------------------------------------------------------- S3 PatchPanel */

const PATCH_COLUMNS: readonly string[] = ["+1 415 •••", "+44 20 ••••", "+1 212 •••"];

const PATCH_ROWS: readonly {
  role: string;
  name: string;
  has: readonly boolean[];
}[] = [
  { role: "ADMIN", name: "Ruth Okafor", has: [true, true, true] },
  { role: "MANAGER", name: "Marcus Webb", has: [true, true, false] },
  { role: "EMPLOYEE", name: "Dana Whitfield", has: [true, false, false] },
  // Priya's one line sits in the SECOND column on purpose: the third number column is
  // hidden below 48rem, and a row whose only lamp is in it would read as "no lines at all"
  // on a phone.
  { role: "EMPLOYEE", name: "Priya Raman", has: [false, true, false] },
];

export function PatchPanel(): React.JSX.Element {
  return (
    <div className="lps-patch" aria-hidden="true">
      <Panel className="lps-patch-panel">
        <div className="lps-patch-head">
          <span className="ex-label">Lines</span>
          <span className="ex-label">Granted per person</span>
        </div>
        <div className="lps-patch-field">
          <div className="lps-patch-grid">
            <span className="lps-patch-corner" />
            {PATCH_COLUMNS.map((c) => (
              <span key={c} className="ex-mono lps-patch-col">
                {c}
              </span>
            ))}
            {PATCH_ROWS.map((r) => (
              <React.Fragment key={r.name}>
                <span className="lps-patch-rowhead">
                  <span className="ex-mono lps-patch-role">{r.role}</span>
                  <span className="lps-patch-name">{r.name}</span>
                </span>
                {r.has.map((on, i) => (
                  <span key={i} className="lps-patch-cell">
                    <span
                      className={clsx("lps-patch-dot", on && "is-on")}
                    />
                  </span>
                ))}
              </React.Fragment>
            ))}
          </div>
        </div>
      </Panel>
    </div>
  );
}

/* ------------------------------------------------------------- S4 Application */

const APPLICATION_CHECKS: readonly {
  label: string;
  state: string;
  waiting?: boolean;
}[] = [
  { label: "Registered details", state: "verified" },
  { label: "Owners", state: "verified" },
  { label: "Ownership document", state: "verified" },
  { label: "ID check · owner 1", state: "verified" },
  { label: "ID check · owner 2", state: "waiting", waiting: true },
];

export function Application(): React.JSX.Element {
  return (
    <div className="lps-app" aria-hidden="true">
      <Panel className="lps-app-panel">
        <div className="lps-app-head">
          <span className="ex-mono lps-app-title">APPLICATION</span>
          <span className="lps-app-status">
            <span className="ex-lamp ex-lamp-wait" />
            <span className="ex-mono lps-app-status-text">IN REVIEW</span>
          </span>
        </div>
        <div className="lps-app-checks">
          {APPLICATION_CHECKS.map((c) => (
            <div key={c.label} className="lps-app-check">
              <span
                className={clsx(
                  "ex-lamp",
                  c.waiting ? "ex-lamp-wait" : "ex-lamp-live",
                )}
              />
              <span className="lps-app-check-label">{c.label}</span>
              <span className="ex-mono lps-app-check-state">{c.state}</span>
            </div>
          ))}
        </div>
        <div className="lps-app-doc">
          <span className="ex-mono lps-app-doc-text">NOT STORED</span>
        </div>
        <div className="lps-app-foot">
          <span className="ex-lamp ex-lamp-live" />
          <span className="lps-app-foot-text">Reviewed by a person</span>
        </div>
      </Panel>
    </div>
  );
}

/*
 * New lps-* class names used in this file, for round 3's surfaces.css:
 *
 *   lps-console            S1 root
 *   lps-console-panel      S1 Panel modifier
 *   lps-console-grid       S1 three-pane grid
 *   lps-rail               S1 left rail
 *   lps-rail-mark          S1 org nameplate block
 *   lps-rail-name          S1 org name (nameplate voice)
 *   lps-rail-lines         S1 stack of line entries
 *   lps-rail-line          S1 one line entry
 *   lps-rail-frag          S1 mono number fragment
 *   lps-threadlist         S1 middle pane
 *   lps-threadlist-head    S1 middle pane header
 *   lps-threadrow          S1 one thread row
 *   lps-threadrow-body     S1 name + preview column
 *   lps-threadrow-name     S1 contact name
 *   lps-threadrow-preview  S1 one-line preview (ellipsis)
 *   lps-threadrow-time     S1 right-aligned mono timestamp
 *   lps-open               S1 right pane
 *   lps-open-head          S1 open-thread header
 *   lps-open-name          S1 open-thread contact name
 *   lps-open-number        S1 open-thread number (mono)
 *   lps-open-timeline      S1 open-thread timeline
 *
 *   lps-thread             S2 root
 *   lps-thread-panel       S2 Panel modifier
 *   lps-thread-head        S2 header row
 *   lps-thread-name        S2 contact name
 *   lps-thread-number      S2 number (mono)
 *   lps-thread-timeline    S2 timeline column
 *
 *   lps-patch              S3 root
 *   lps-patch-panel        S3 Panel modifier
 *   lps-patch-head         S3 header row
 *   lps-patch-field        S3 recessed inner field
 *   lps-patch-grid         S3 matrix grid
 *   lps-patch-corner       S3 empty top-left cell
 *   lps-patch-col          S3 column header (mono)
 *   lps-patch-rowhead      S3 row header cell
 *   lps-patch-role         S3 role label (mono)
 *   lps-patch-name         S3 person name
 *   lps-patch-cell         S3 one matrix cell
 *   lps-patch-dot          S3 lamp / empty ring; `.is-on` = verdigris lamp
 *
 *   lps-app                S4 root
 *   lps-app-panel          S4 Panel modifier
 *   lps-app-head           S4 header row
 *   lps-app-title          S4 "APPLICATION" (mono)
 *   lps-app-status         S4 status pill
 *   lps-app-status-text    S4 "IN REVIEW" (mono)
 *   lps-app-checks         S4 checklist
 *   lps-app-check          S4 one checklist row
 *   lps-app-check-label    S4 checklist label
 *   lps-app-check-state    S4 checklist state (mono)
 *   lps-app-doc            S4 redacted document block
 *   lps-app-doc-text       S4 "NOT STORED" (mono)
 *   lps-app-foot           S4 footer row
 *   lps-app-foot-text      S4 footer text
 *
 * Modifier classes used here: `.is-active`, `.is-selected`, `.is-on`.
 * Reused from authTheme.css: `.ex-label`, `.ex-mono`, `.ex-lamp`, `.ex-lamp-live`,
 * `.ex-lamp-wait`, `.ex-nameplate`.
 * Reused from panel.tsx: `.lps-panel`, `.lps-filament`, `.lps-entry`, `.lps-glyph`,
 * `.lps-name`, `.lps-time`, `.lps-meta`, `.lps-state`, `.lps-bubble`,
 * `.lps-bubble-body`, `.lps-wave`, `.lps-wave-bar`, `.lps-day`, `.lps-day-rule`,
 * `.lps-day-label`.
 */
