/**
 * The first screen. Fourteen figures and no score.
 *
 * The absence is the design. There is no field here that combines the others,
 * because the categories have different denominators and different evidence
 * behind them, and a single number would be read as though they did not. What
 * the screen shows instead is the shape of the evidence: how much applied, how
 * much was stable, how much nobody measured, and what the whole thing does not
 * establish.
 */

import { Field, FieldList, Panel, Stat, StatList, ViewHeader } from '@/components/primitives';
import type { ViewerDataset } from '@/data/types';
import { transportLabel } from '@/data/selectors';
import { hrefFor } from '@/app/routes';

export function Overview({
  dataset,
  headingRef,
}: {
  dataset: ViewerDataset;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const { overview, benchmark, project, provenance, measurement_coverage: coverage } = dataset;
  const notMeasuredMetrics = coverage.entries.filter((entry) => entry.state === 'not_measured');
  const partialMetrics = coverage.entries.filter(
    (entry) => entry.state === 'partially_measured',
  );

  return (
    <>
      <ViewHeader
        eyebrow={provenance.eyebrow}
        title={project.name}
        lede={<p>{project.tagline}</p>}
        headingRef={headingRef}
      />

      <p>{project.problem}</p>

      <section className="section" aria-labelledby="overview-scope">
        <h2 className="section__title" id="overview-scope">
          What was measured
        </h2>
        <div className="grid grid--4">
          <Stat
            label="Implementations"
            value={overview.target_count}
            note="pinned, contained"
          />
          <StatList label="Languages" values={overview.languages} />
          <StatList label="Transports" values={overview.transports.map(transportLabel)} />
          <StatList label="Profiles" values={overview.profiles} />
          <Stat label="Scenarios" value={overview.scenario_count} note="per implementation" />
          <Stat
            label="Repetitions"
            value={overview.correctness_repetitions}
            note="complete-signature equality"
          />
        </div>
      </section>

      <section className="section" aria-labelledby="overview-results">
        <h2 className="section__title" id="overview-results">
          What came out
        </h2>
        <div className="grid grid--4">
          <Stat
            label="Applicable results"
            value={overview.applicable_results}
            note="the denominator for every rate"
          />
          <Stat label="Stable pass" value={overview.stable_pass} />
          <Stat label="Stable non-pass" value={overview.stable_non_pass} />
          <Stat
            label="Inconclusive"
            value={overview.inconclusive}
            note="repetitions disagreed"
          />
          <Stat
            label="Not applicable"
            value={overview.not_applicable}
            note="outside every denominator"
            absent
          />
          {/*
           * "Scenario results not measured", never a bare "Not measured". The
           * same words describe an un-run scenario and an un-taken timing, and
           * a reader who met them on one screen carried the wrong meaning to
           * the other. Each label now names its own domain.
           */}
          <Stat
            label="Scenario results not measured"
            value={overview.not_measured}
            note="no result recorded for the cell"
            absent
          />
          <Stat
            label="Security-relevant"
            value={overview.security_relevant_observations}
            note="observations, not vulnerabilities"
          />
          <Stat
            label="Confirmed vulnerabilities"
            value={overview.confirmed_private_vulnerabilities}
            note="human-reached; no code writes this"
          />
        </div>
        <p className="callout" style={{ marginTop: 'var(--space-4)' }}>
          There is no overall score on this page and none can be derived from it. Each category
          has its own applicable denominator and its own limitations, and the rows of the matrix
          are not comparable as wholes — a target with a narrower applicable set is not a better
          one. See <a href={hrefFor({ name: 'methodology' })}>methodology</a>.
        </p>
      </section>

      <section className="section" aria-labelledby="overview-coverage">
        <h2 className="section__title" id="overview-coverage">
          Measurement coverage
        </h2>
        <div className="grid grid--4">
          <Stat
            label="Timing metrics measured"
            value={coverage.entries.filter((entry) => entry.state === 'measured').length}
          />
          <Stat label="Timing metrics partially measured" value={partialMetrics.length} />
          <Stat
            label="Timing metrics not measured"
            value={notMeasuredMetrics.length}
            note="no sample taken"
            absent
          />
          <Stat label="Promised metrics" value={coverage.entries.length} />
        </div>
        <p className="callout callout--absent" style={{ marginTop: 'var(--space-4)' }}>
          {notMeasuredMetrics.length} of the {coverage.entries.length} timing dimensions this
          methodology names were not measured, and each states why on the{' '}
          <a href={hrefFor({ name: 'timings' })}>timings</a> screen. This is a statement about
          the <strong>timing</strong> metrics, not about the scenario results above: a metric
          nobody sampled is never a failure and never a zero.
        </p>
      </section>

      <section className="section" aria-labelledby="overview-approach">
        <h2 className="section__title" id="overview-approach">
          How
        </h2>
        <ul>
          {project.approach.map((line) => (
            <li key={line} style={{ marginBottom: 'var(--space-2)', maxWidth: '70ch' }}>
              {line}
            </li>
          ))}
        </ul>
      </section>

      <section className="section" aria-labelledby="overview-not-claimed">
        <h2 className="section__title" id="overview-not-claimed">
          What this does not claim
        </h2>
        <ul>
          {project.not_claimed.map((line) => (
            <li
              key={line}
              style={{
                marginBottom: 'var(--space-2)',
                maxWidth: '70ch',
                color: 'var(--text-secondary)',
              }}
            >
              {line}
            </li>
          ))}
        </ul>
      </section>

      {/*
       * Three provenance questions, three panels, deliberately not merged. The
       * schema is shared between a synthetic build and the private benchmark;
       * the measurements are not. One combined "generated from" list was how a
       * synthetic matrix came to sit under the private benchmark's name.
       */}
      <section className="section" aria-labelledby="overview-provenance">
        <h2 className="section__title" id="overview-provenance">
          Provenance
        </h2>

        <Panel quiet title="Schema and exporter">
          <FieldList>
            <Field label="Lineage" value={provenance.schema_lineage} prose />
            <Field label="Benchmark schema" value={provenance.benchmark_schema_version} />
            <Field label="Viewer dataset schema" value={provenance.viewer_schema_version} />
            <Field label="Harness version" value={provenance.harness_version} />
          </FieldList>
        </Panel>

        <Panel quiet title="The data on this screen">
          <FieldList>
            <Field
              label="Evidence"
              value={
                provenance.evidence_kind === 'synthetic'
                  ? 'Synthetic — invented, not measured'
                  : 'Real measurement'
              }
            />
            <Field label="What that means" value={provenance.evidence_note} prose />
            <Field
              label="Benchmark manifest digest"
              value={dataset.generated_from_benchmark_manifest_hash}
            />
            <Field label="Dataset generated" value={dataset.generated_at} />
          </FieldList>
        </Panel>

        <Panel quiet title="Private benchmark identity">
          <FieldList>
            <Field
              label="Benchmark"
              value={benchmark.benchmark_name}
              prose
              absentNote={
                provenance.benchmark_identity_withheld
                  ? 'withheld in public-safe mode'
                  : 'not applicable — no private execution produced this document'
              }
            />
            <Field label="Status" value={provenance.benchmark_identity_note} prose />
            <Field
              label="Definition pinned at"
              value={benchmark.definition_harness_commit}
              absentNote="withheld in public-safe mode"
            />
            <Field
              label="Execution ran at"
              value={benchmark.execution_harness_commit}
              absentNote="withheld in public-safe mode"
            />
            <Field label="Visibility" value={dataset.visibility_mode} />
          </FieldList>
        </Panel>
      </section>
    </>
  );
}
