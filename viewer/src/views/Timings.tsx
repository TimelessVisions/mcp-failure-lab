/**
 * Timings, and — with equal weight — the timings that do not exist.
 *
 * A timings screen showing only what was measured invites a reader to count the
 * cards and conclude that is the whole set. It is not: this methodology names
 * sixteen dimensions and several of them have no boundary to time. So the
 * coverage ledger leads the screen, every un-measured metric gets a card of its
 * own carrying its reason, and no card ever shows a median without the samples
 * behind it.
 *
 * Nothing here compares two implementations' durations. A comparison between
 * languages measures the languages and their runtimes as much as anything else.
 */

import {
  CoverageMeter,
  Field,
  FieldList,
  Limitations,
  Panel,
  SampleStrip,
  ViewHeader,
} from '@/components/primitives';
import { hrefFor } from '@/app/routes';
import { formatMs, measuredTimings, unmeasuredTimings } from '@/data/selectors';
import type { MeasurementCoverageEntry, TimingCard, ViewerDataset } from '@/data/types';

const COVERAGE_LABEL: Record<string, string> = {
  measured: 'measured',
  partially_measured: 'partially measured',
  not_measured: 'not measured',
  not_applicable: 'not applicable',
};

export function Timings({
  dataset,
  headingRef,
}: {
  dataset: ViewerDataset;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const measured = measuredTimings(dataset);
  const unmeasured = unmeasuredTimings(dataset);
  const ledger = dataset.measurement_coverage;

  return (
    <>
      <ViewHeader
        eyebrow="Timings"
        title="Like-for-like measurements, and everything not measured"
        lede={
          <p>
            Median, minimum, maximum and every individual sample. No confidence interval and no
            significance claim — a handful of samples on a shared runner cannot support either.
            Durations are never compared between implementations: that comparison measures the
            languages and their runtimes as much as anything else.
          </p>
        }
        headingRef={headingRef}
      />

      <section className="section" aria-labelledby="coverage-ledger">
        <h2 className="section__title" id="coverage-ledger">
          Measurement coverage ledger
        </h2>
        <p>
          Every timing dimension this methodology names, with its real state. A metric is
          measured here because samples exist on disk for it, never because it was promised.
        </p>
        <Panel quiet>
          {ledger.entries.map((entry) => (
            <LedgerRow key={entry.metric_id} entry={entry} />
          ))}
        </Panel>
        <Panel quiet>
          <Limitations items={ledger.limitations} title="What the ledger does not establish" />
        </Panel>
      </section>

      <section className="section" aria-labelledby="measured-timings">
        <h2 className="section__title" id="measured-timings">
          Measured
        </h2>
        <div className="grid grid--2">
          {measured.map((card) => (
            <TimingPanel key={`${card.metric_id ?? ''}${card.name}`} card={card} />
          ))}
        </div>
        {measured.length === 0 && (
          <p className="callout callout--absent">This execution produced no timing at all.</p>
        )}
      </section>

      <section className="section" aria-labelledby="unmeasured-timings">
        <h2 className="section__title" id="unmeasured-timings">
          Timing metrics not measured
        </h2>
        <p>
          Present on this screen deliberately. An un-measured metric is never a failure, never a
          zero, and never omitted. These are <strong>timing</strong> metrics: a scenario with no
          recorded result is a different state, counted separately on the{' '}
          <a href={hrefFor({ name: 'overview' })}>overview</a>.
        </p>
        <div className="grid grid--2">
          {unmeasured.map((card) => (
            <TimingPanel key={`${card.metric_id ?? ''}${card.name}`} card={card} />
          ))}
        </div>
      </section>

      <section className="section" aria-labelledby="overhead">
        <h2 className="section__title" id="overhead">
          Containment overhead
        </h2>
        <p>
          Measured with the implementation, the scenario and the profile held constant, so the
          only difference is containment. Never inferred by comparing two different
          implementations: the gap between a Go binary and a Node server is not the cost of a
          container.
        </p>
        <div className="grid grid--2">
          {dataset.containment_overhead.map((pair) => (
            <Panel key={pair.transport} title={pair.transport}>
              {pair.state !== 'measured' ? (
                <>
                  <p className="callout callout--absent">{pair.reason ?? 'not measured'}</p>
                  <Limitations items={pair.limitations} />
                </>
              ) : (
                <>
                  <FieldList>
                    <Field label="Scenario held constant" value={pair.scenario} />
                    <Field label="Host median" value={formatMs(pair.host?.median_ms ?? null)} />
                    <Field
                      label="Contained median"
                      value={formatMs(pair.contained?.median_ms ?? null)}
                    />
                    <Field
                      label="Ratio"
                      value={pair.overhead_ratio === null ? null : `${pair.overhead_ratio}×`}
                    />
                    <Field
                      label="Absolute"
                      value={
                        pair.overhead_absolute_ms === null
                          ? null
                          : formatMs(pair.overhead_absolute_ms)
                      }
                    />
                  </FieldList>
                  <OverheadBars pair={pair} />
                  <Limitations items={pair.limitations} />
                </>
              )}
            </Panel>
          ))}
        </div>
      </section>
    </>
  );
}

function LedgerRow({ entry }: { entry: MeasurementCoverageEntry }) {
  const family =
    entry.state === 'measured'
      ? 'pass'
      : entry.state === 'partially_measured'
        ? 'uncertain'
        : 'absent';
  return (
    <div className="ledger-row">
      <div>
        <div>
          <strong>{entry.display_name}</strong>{' '}
          <span className="mono" style={{ color: 'var(--text-quiet)' }}>
            {entry.metric_id}
          </span>
        </div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.8125rem', margin: 0 }}>
          {entry.measurement_method}
        </p>
        {entry.reason_not_measured !== null && (
          <p style={{ color: 'var(--text-tertiary)', fontSize: '0.75rem', margin: 0 }}>
            {entry.reason_not_measured}
          </p>
        )}
        <p style={{ color: 'var(--text-quiet)', fontSize: '0.75rem', margin: 0 }}>
          {entry.environment} · {entry.limitation}
        </p>
      </div>
      <div style={{ textAlign: 'right' }}>
        <span className={`badge badge--${family}`}>{COVERAGE_LABEL[entry.state]}</span>
        <div className="mono" style={{ fontSize: '0.6875rem', marginTop: 'var(--space-1)' }}>
          {entry.sample_count} / {entry.minimum_required_samples} samples
        </div>
        <div className="mono" style={{ fontSize: '0.6875rem', color: 'var(--text-quiet)' }}>
          {entry.warmup_count} warm-up · {entry.artifact_reference ?? 'no artifact'}
        </div>
        <CoverageMeter entry={entry} />
      </div>
    </div>
  );
}

function TimingPanel({ card }: { card: TimingCard }) {
  return (
    <Panel title={card.name}>
      <FieldList>
        <Field label="State" value={card.state} />
        <Field label="Samples" value={card.sample_count} />
        <Field label="Warm-ups excluded" value={card.warmup_count} />
        <Field
          label="Median"
          value={card.median_ms === null ? null : formatMs(card.median_ms)}
          absentNote={card.insufficient_reason ?? 'no headline figure from these samples'}
        />
        <Field
          label="Minimum"
          value={card.min_ms === null ? null : formatMs(card.min_ms)}
          absentNote="—"
        />
        <Field
          label="Maximum"
          value={card.max_ms === null ? null : formatMs(card.max_ms)}
          absentNote="—"
        />
        <Field
          label="Single observation"
          value={card.single_value_ms === null ? null : formatMs(card.single_value_ms)}
          absentNote="not a single-observation measurement"
        />
        <Field
          label="Individual samples"
          value={card.samples_ms.map((value) => formatMs(value)).join(' · ')}
          absentNote="none recorded"
        />
        <Field
          label="Warm-up samples"
          value={card.warmup_samples_ms.map((value) => formatMs(value)).join(' · ')}
          absentNote="none recorded"
        />
        <Field label="Rejected samples" value={card.rejected_samples} />
        <Field label="Environment" value={card.environment} absentNote="unrecorded" />
      </FieldList>
      <SampleStrip card={card} />
      <Limitations items={card.limitations} />
    </Panel>
  );
}

function OverheadBars({ pair }: { pair: ViewerDataset['containment_overhead'][number] }) {
  const host = pair.host?.median_ms ?? null;
  const contained = pair.contained?.median_ms ?? null;
  if (host === null || contained === null) return null;
  const peak = Math.max(host, contained);
  return (
    <div className="ratio-bars">
      {(
        [
          ['host', host],
          ['contained', contained],
        ] as const
      ).map(([label, value]) => (
        <div className="ratio-bar" key={label}>
          <span>{label}</span>
          <span className="ratio-bar__track">
            <span className="ratio-bar__fill" style={{ width: `${(value / peak) * 100}%` }} />
          </span>
          <span className="mono" style={{ textAlign: 'right' }}>
            {formatMs(value)}
          </span>
        </div>
      ))}
    </div>
  );
}
