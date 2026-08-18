/**
 * The small pieces every view is built from.
 *
 * Two of them carry rules rather than styling. `Field` renders an absent value
 * as an explicit "not recorded" instead of an empty cell, because a blank space
 * reads as zero and zero is a measurement. `Limitations` refuses to render
 * nothing: a panel with no limitations listed is a panel claiming there are
 * none, so the caller has to supply them.
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';

import type {
  CellState,
  DatasetProvenance,
  MatrixCell,
  MeasurementCoverageEntry,
  TimingCard,
  ViewerScenario,
} from '@/data/types';
import { STATE_MEANING, formatMs, stateFamily } from '@/data/selectors';

/**
 * A horizontally scrolling container that a keyboard can reach.
 *
 * Every wide table in this viewer scrolls inside its own box rather than
 * pushing the page sideways. A scroll box that cannot take focus is
 * unreachable without a pointer, so this one is focusable and named — which is
 * also what stops it being an unlabelled landmark to a screen reader.
 */
export function ScrollRegion({ label, children }: { label: string; children: ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [overflowing, setOverflowing] = useState(false);

  // Measured, not assumed. A hint that is always shown is noise on the
  // majority of viewports where the table already fits, and one that is never
  // shown leaves the reader to discover the hidden columns by accident.
  useEffect(() => {
    const element = ref.current;
    if (element === null) return;
    const check = () => setOverflowing(element.scrollWidth > element.clientWidth + 1);
    check();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(check);
    observer.observe(element);
    return () => observer.disconnect();
  }, [children]);

  return (
    <div className="scroll-region">
      <div
        className="matrix-wrap"
        ref={ref}
        tabIndex={0}
        role="region"
        aria-label={overflowing ? `${label}. Scrolls horizontally.` : label}
        data-overflowing={overflowing ? 'true' : 'false'}
      >
        {children}
      </div>
      {overflowing && (
        <p className="scroll-region__hint">
          <span aria-hidden="true">↔</span> This table is wider than the screen — scroll it
          sideways, or use arrow keys once it has focus, to reach the remaining columns.
        </p>
      )}
    </div>
  );
}

export function Panel({
  title,
  children,
  quiet = false,
}: {
  title?: ReactNode;
  children: ReactNode;
  quiet?: boolean;
}) {
  return (
    <section className={quiet ? 'panel panel--quiet' : 'panel'}>
      {title !== undefined && <h3 className="section__title">{title}</h3>}
      {children}
    </section>
  );
}

export function Field({
  label,
  value,
  prose = false,
  absentNote = 'not recorded',
}: {
  label: string;
  value: ReactNode;
  prose?: boolean;
  absentNote?: string;
}) {
  const absent = value === null || value === undefined || value === '';
  const classes = ['field__value'];
  if (absent) classes.push('field__value--absent');
  else if (prose) classes.push('field__value--prose');
  return (
    <div className="field">
      <dt className="field__label">{label}</dt>
      <dd className={classes.join(' ')}>{absent ? absentNote : value}</dd>
    </div>
  );
}

export function FieldList({ children }: { children: ReactNode }) {
  return <dl className="field-list">{children}</dl>;
}

export function Stat({
  label,
  value,
  note,
  absent = false,
}: {
  label: string;
  value: ReactNode;
  note?: string;
  absent?: boolean;
}) {
  return (
    <div className={absent ? 'stat stat--absent' : 'stat'}>
      <div className="stat__label">{label}</div>
      <div className="stat__value">{value}</div>
      {note !== undefined && <div className="stat__note">{note}</div>}
    </div>
  );
}

/**
 * A stat whose value is a list of labels rather than one number.
 *
 * Each label gets its own line. The previous treatment joined them with a
 * separator into a single mono string inside a card with `overflow: hidden`,
 * so a two-transport execution rendered as "stdio · streamable_h" — a clipped
 * value that reads as a complete one.
 */
export function StatList({
  label,
  values,
  note,
  empty = '—',
}: {
  label: string;
  values: string[];
  note?: string;
  empty?: string;
}) {
  return (
    <div className="stat">
      <div className="stat__label">{label}</div>
      <div
        className={
          values.length > 1 ? 'stat__value stat__values stat__values--multi' : 'stat__value'
        }
      >
        {values.length === 0 ? (
          <span>{empty}</span>
        ) : (
          values.map((value) => <span key={value}>{value}</span>)
        )}
      </div>
      {note !== undefined && <div className="stat__note">{note}</div>}
    </div>
  );
}

export function StateBadge({ state, title }: { state: CellState; title?: string }) {
  return (
    <span
      className={`badge badge--${stateFamily(state)}`}
      title={title ?? STATE_MEANING[state]}
      data-state={state}
    >
      {state}
    </span>
  );
}

export function SecurityBadge({ status }: { status: string }) {
  return (
    <span
      className="badge badge--security"
      title="Security relevance is a separate dimension from the primary outcome. This says what has actually been established about exploiting it."
    >
      security-relevant · {status}
    </span>
  );
}

/**
 * Three security questions that are not the same question.
 *
 * A scenario can be *written* to probe a security property; a run can *observe*
 * something security-relevant; and a human can *confirm* a vulnerability. Only
 * the third is a finding, and nothing in this harness writes it. Collapsing
 * them — as a single "Security dimension" line did — makes a security-oriented
 * scenario that did not even apply read like a discovered weakness, which is
 * the most damaging thing a tool like this can get wrong.
 */
export function SecurityDimension({
  cell,
  scenario,
}: {
  cell: MatrixCell;
  scenario: ViewerScenario | undefined;
}) {
  const orientedCategory = scenario?.security_category ?? null;
  const applied = cell.state !== 'NOT_APPLICABLE' && cell.state !== 'NOT_MEASURED';

  return (
    <div className="security-dimension">
      <FieldList>
        <Field
          label="Scenario purpose"
          value={
            orientedCategory === null
              ? 'not a security-oriented scenario'
              : `security-oriented — ${orientedCategory}`
          }
        />
        <Field
          label="Observation in this run"
          value={
            !applied
              ? 'none — the scenario produced no result here'
              : cell.security_relevant
                ? `security-relevant observation — ${cell.security_category ?? 'uncategorised'}`
                : 'no security-relevant observation'
          }
        />
        <Field
          label="Vulnerability"
          value={
            cell.vulnerability_status === 'not_assessed'
              ? 'not assessed — no execution path in this harness confirms one'
              : cell.vulnerability_status
          }
        />
      </FieldList>

      {orientedCategory !== null && !applied && (
        <p className="callout callout--absent">
          This scenario was written to probe a security property, and it did not apply to this
          implementation. That is a statement about the scenario&apos;s purpose, not a finding:
          nothing was observed here, and nothing is claimed about this implementation&apos;s
          security.
        </p>
      )}
    </div>
  );
}

/**
 * A panel's stated limitations. Never optional.
 *
 * Rendering nothing when the list is empty would let a limitation-free panel
 * pass silently, and every figure in this dataset reads stronger than the
 * evidence behind it.
 */
export function Limitations({
  items,
  title = 'What this does not establish',
}: {
  items: string[];
  title?: string;
}) {
  if (items.length === 0) {
    return (
      <p className="limitations">
        No limitations were recorded for this value, which is itself a gap in the evidence
        rather than a statement that none apply.
      </p>
    );
  }
  return (
    <>
      <p className="limitations__title">{title}</p>
      <ul className="limitations">
        {items.map((text) => (
          <li key={text}>{text}</li>
        ))}
      </ul>
    </>
  );
}

/**
 * Every individual sample behind a timing, drawn as a strip.
 *
 * The bars are scaled to the maximum of the samples *including* the warm-ups,
 * so the excluded warm-up is visible beside the measured values rather than
 * being a claim in a caption. A timing with no samples renders no strip at all —
 * a flat baseline would read as five measurements of zero.
 */
export function SampleStrip({ card }: { card: TimingCard }) {
  const all = [...card.warmup_samples_ms, ...card.samples_ms];
  if (all.length === 0) return null;
  const peak = Math.max(...all);
  const scale = (value: number) => `${Math.max(4, (value / peak) * 100)}%`;

  return (
    <div className="samples" role="img" aria-label={sampleStripLabel(card)}>
      {card.warmup_samples_ms.map((value, index) => (
        <div
          key={`warmup-${index}`}
          className="samples__bar samples__bar--warmup"
          style={{ height: scale(value), animationDelay: `${index * 40}ms` }}
        />
      ))}
      {card.samples_ms.map((value, index) => (
        <div
          key={`sample-${index}`}
          className="samples__bar"
          style={{
            height: scale(value),
            animationDelay: `${(card.warmup_samples_ms.length + index) * 40}ms`,
          }}
        />
      ))}
    </div>
  );
}

export function sampleStripLabel(card: TimingCard): string {
  const warmup =
    card.warmup_samples_ms.length > 0
      ? `${card.warmup_samples_ms.length} excluded warm-up, `
      : '';
  return (
    `${card.name}: ${warmup}${card.samples_ms.length} measured samples ` +
    `from ${formatMs(card.min_ms)} to ${formatMs(card.max_ms)}`
  );
}

/** A coverage entry's state as ticks: filled, partial, or empty. */
export function CoverageMeter({ entry }: { entry: MeasurementCoverageEntry }) {
  const total = Math.max(entry.minimum_required_samples, entry.sample_count, 1);
  const ticks = Array.from({ length: Math.min(total, 12) }, (_, index) => index);
  const filled = Math.min(entry.sample_count, ticks.length);
  return (
    <div
      className="coverage-meter"
      role="img"
      aria-label={`${entry.sample_count} of ${entry.minimum_required_samples} samples required for a headline figure`}
    >
      {ticks.map((index) => (
        <span
          key={index}
          className={
            index < filled
              ? entry.state === 'measured'
                ? 'coverage-meter__tick coverage-meter__tick--filled'
                : 'coverage-meter__tick coverage-meter__tick--partial'
              : 'coverage-meter__tick'
          }
        />
      ))}
    </div>
  );
}

/**
 * What the numbers on this screen actually are, stated before any of them.
 *
 * Rendered from the dataset rather than from a build flag, so a document cannot
 * be served under a label its own provenance does not support. It is a `note`
 * landmark rather than an `alert`: it is a standing property of the document,
 * not an event, and an alert would interrupt a screen reader on every route.
 */
export function EvidenceDisclaimer({ provenance }: { provenance: DatasetProvenance }) {
  if (provenance.disclaimer === null) return null;
  return (
    <aside
      className="disclaimer"
      role="note"
      aria-label="What this evidence is"
      data-evidence-kind={provenance.evidence_kind}
    >
      <p className="disclaimer__title">{provenance.eyebrow}</p>
      <p className="disclaimer__body">{provenance.disclaimer}</p>
    </aside>
  );
}

export function ViewHeader({
  eyebrow,
  title,
  lede,
  headingRef,
}: {
  eyebrow: string;
  title: string;
  lede: ReactNode;
  headingRef?: React.RefObject<HTMLHeadingElement | null>;
}) {
  return (
    <header className="view-header">
      <p className="eyebrow">{eyebrow}</p>
      <h1 className="view-title" tabIndex={-1} ref={headingRef}>
        {title}
      </h1>
      <div className="view-lede">{lede}</div>
    </header>
  );
}
