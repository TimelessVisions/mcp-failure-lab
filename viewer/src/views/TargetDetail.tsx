/**
 * Everything known about one implementation, including what is not known.
 *
 * The "not measured" surface has a section of its own rather than being absent
 * from the page. A detail screen that listed only what was observed would let a
 * reader take the list as complete, which is the single most likely way this
 * evidence gets misread.
 */

import {
  CoverageMeter,
  Field,
  FieldList,
  Limitations,
  Panel,
  ScrollRegion,
  Stat,
  StateBadge,
  ViewHeader,
} from '@/components/primitives';
import { hrefFor } from '@/app/routes';
import {
  capabilityFor,
  categoriesForTarget,
  cellsForTarget,
  containmentFor,
  formatMs,
} from '@/data/selectors';
import type { CellState, ViewerDataset } from '@/data/types';

export function TargetDetail({
  dataset,
  targetKey,
  headingRef,
}: {
  dataset: ViewerDataset;
  targetKey: string;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const target = dataset.targets.find((entry) => entry.target_key === targetKey);
  if (target === undefined) {
    return (
      <ViewHeader
        eyebrow="Implementation"
        title="No such implementation in this dataset"
        lede={
          <p>
            This dataset describes {dataset.targets.length} implementations and{' '}
            <span className="mono">{targetKey}</span> is not one of them.{' '}
            <a href={hrefFor({ name: 'matrix' })}>Back to the matrix</a>.
          </p>
        }
        headingRef={headingRef}
      />
    );
  }

  const cells = cellsForTarget(dataset, targetKey);
  const categories = categoriesForTarget(dataset, targetKey);
  const surface = capabilityFor(dataset, targetKey);
  const containment = containmentFor(dataset, targetKey);
  const timings = dataset.timings.filter((card) => card.name.includes(target.display_name));

  const distribution = new Map<CellState, number>();
  for (const cell of cells) {
    distribution.set(cell.state, (distribution.get(cell.state) ?? 0) + 1);
  }

  const unmeasured = dataset.measurement_coverage.entries.filter(
    (entry) => entry.state !== 'measured',
  );

  return (
    <>
      <ViewHeader
        eyebrow="Implementation"
        title={target.display_name}
        lede={
          <p>
            {target.language} · {target.transport} · negotiated{' '}
            {target.negotiated_profile ?? target.declared_profile} · {target.relationship}
          </p>
        }
        headingRef={headingRef}
      />

      <section className="section" aria-labelledby="target-identity">
        <h2 className="section__title" id="target-identity">
          Pinned identity
        </h2>
        <Panel quiet>
          <FieldList>
            <Field
              label="Target id"
              value={target.target_id}
              absentNote="withheld — identity is what re-identifies a third party"
            />
            <Field label="Target definition" value={target.target_file} absentNote="withheld" />
            <Field label="Version" value={target.source_version} absentNote="withheld" />
            <Field label="Source commit" value={target.source_commit} absentNote="withheld" />
            <Field label="Relationship" value={target.relationship} />
            <Field label="Declared profile" value={target.declared_profile} />
            <Field
              label="Negotiated profile"
              value={target.negotiated_profile}
              absentNote="no handshake was observed in this execution"
            />
            <Field label="Transport" value={target.transport} />
            <Field
              label="Portable build identity"
              value={target.portable_identity_fingerprint}
              absentNote="withheld"
            />
            <Field
              label="Target fingerprint"
              value={target.target_fingerprint}
              absentNote="withheld"
            />
            <Field
              label="Dockerfile sha256"
              value={target.dockerfile_sha256}
              absentNote="withheld"
            />
            <Field
              label="Dependency lock sha256"
              value={target.lock_sha256}
              absentNote="withheld"
            />
            <Field
              label="Capability fingerprint"
              value={target.capability_fingerprint}
              absentNote="withheld"
            />
            <Field
              label="Official conformance"
              value={`${target.official_conformance} — ${target.official_conformance_detail ?? 'no detail recorded'}`}
              prose
            />
          </FieldList>
          <Limitations items={target.limitations} />
        </Panel>
      </section>

      <section className="section" aria-labelledby="target-distribution">
        <h2 className="section__title" id="target-distribution">
          Classification distribution
        </h2>
        <div className="grid grid--4">
          {[...distribution.entries()]
            .sort((a, b) => b[1] - a[1])
            .map(([state, count]) => (
              <Stat
                key={state}
                label={state}
                value={count}
                absent={state === 'NOT_APPLICABLE' || state === 'NOT_MEASURED'}
              />
            ))}
        </div>
        <p className="callout" style={{ marginTop: 'var(--space-4)' }}>
          These counts describe this implementation's own applicable set. They are not
          comparable as a whole against another implementation's, whose applicable set is a
          different size for reasons of transport and advertised surface.
        </p>
      </section>

      <section className="section" aria-labelledby="target-categories">
        <h2 className="section__title" id="target-categories">
          Applicable scenarios by category
        </h2>
        <ScrollRegion label="Applicable scenarios by category, scrollable">
          <table>
            <caption>The denominator for every rate is the applicable column.</caption>
            <thead>
              <tr>
                <th scope="col">Category</th>
                <th scope="col" className="numeric">
                  Applicable
                </th>
                <th scope="col" className="numeric">
                  Stable pass
                </th>
                <th scope="col" className="numeric">
                  Stable non-pass
                </th>
                <th scope="col" className="numeric">
                  Inconclusive
                </th>
                <th scope="col" className="numeric">
                  Not applicable
                </th>
              </tr>
            </thead>
            <tbody>
              {categories.map((summary) => (
                <tr key={summary.category}>
                  <th scope="row" style={{ fontWeight: 400 }}>
                    <a href={hrefFor({ name: 'categories', category: summary.category })}>
                      {summary.category}
                    </a>
                  </th>
                  <td className="numeric">{summary.applicable_scenarios}</td>
                  <td className="numeric">{summary.stable_pass}</td>
                  <td className="numeric">{summary.stable_non_pass}</td>
                  <td className="numeric">{summary.inconclusive}</td>
                  <td className="numeric">{summary.not_applicable}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollRegion>
      </section>

      <section className="section" aria-labelledby="target-surface">
        <h2 className="section__title" id="target-surface">
          Capability surface
        </h2>
        {surface === undefined ? (
          <p className="callout callout--absent">
            No capability record was written for this implementation in this execution.
          </p>
        ) : (
          <Panel quiet>
            <FieldList>
              <Field label="Resolution status" value={surface.status} />
              <Field label="Advertised tools" value={surface.tools.length} />
              <Field
                label="Tool names"
                value={surface.tools.map((tool) => tool.name).join(', ')}
                prose
              />
              <Field
                label="Declared in the target file"
                value={surface.declared_tools.join(', ')}
                absentNote="the target file declares none"
                prose
              />
              <Field
                label="Observed transport features"
                value={Object.entries(surface.observed_transport_features)
                  .map(([key, value]) => `${key}=${value}`)
                  .join(' · ')}
                absentNote="none observed"
              />
            </FieldList>
            <p style={{ marginTop: 'var(--space-3)' }}>
              <a href={hrefFor({ name: 'capabilities' })}>Full capability surface →</a>
            </p>
          </Panel>
        )}
      </section>

      <section className="section" aria-labelledby="target-containment">
        <h2 className="section__title" id="target-containment">
          Containment evidence
        </h2>
        {containment === undefined ? (
          <p className="callout callout--absent">
            No containment record was written for this implementation. Absent evidence, not weak
            containment — nothing here says it ran uncontained.
          </p>
        ) : (
          <Panel quiet>
            <FieldList>
              <Field label="Mode" value={containment.mode} />
              <Field label="Outcome" value={containment.outcome} />
              <Field label="Usable as evidence" value={containment.usable ? 'yes' : 'no'} />
              <Field
                label="Image source"
                value={containment.image_source_kind}
                absentNote="withheld"
              />
            </FieldList>
            <p style={{ marginTop: 'var(--space-3)' }}>
              <a href={hrefFor({ name: 'containment' })}>Requested against observed →</a>
            </p>
          </Panel>
        )}
      </section>

      <section className="section" aria-labelledby="target-timings">
        <h2 className="section__title" id="target-timings">
          Timing measurements
        </h2>
        {timings.length === 0 ? (
          <p className="callout callout--absent">
            No timing in this execution is attributed to this implementation by name.
          </p>
        ) : (
          <div className="grid grid--2">
            {timings.map((card) => (
              <Panel key={card.name} quiet>
                <FieldList>
                  <Field label="Measurement" value={card.name} prose />
                  <Field label="State" value={card.state} />
                  <Field
                    label="Median"
                    value={card.median_ms === null ? null : formatMs(card.median_ms)}
                    absentNote={card.insufficient_reason ?? 'no headline figure'}
                  />
                  <Field
                    label="Single observation"
                    value={
                      card.single_value_ms === null ? null : formatMs(card.single_value_ms)
                    }
                    absentNote="not a single-observation measurement"
                  />
                  <Field label="Samples" value={card.sample_count} />
                </FieldList>
              </Panel>
            ))}
          </div>
        )}
        <p style={{ marginTop: 'var(--space-3)' }}>
          <a href={hrefFor({ name: 'timings' })}>
            All timings, including what was not measured →
          </a>
        </p>
      </section>

      <section className="section" aria-labelledby="target-unmeasured">
        <h2 className="section__title" id="target-unmeasured">
          The not-measured surface
        </h2>
        <p>
          {unmeasured.length} of the {dataset.measurement_coverage.entries.length} timing
          dimensions this methodology names are not fully measured for any implementation,
          including this one. None of them is a zero and none is a failure.
        </p>
        <Panel quiet>
          {unmeasured.map((entry) => (
            <div className="ledger-row" key={entry.metric_id}>
              <div>
                <div className="mono">{entry.display_name}</div>
                <p style={{ color: 'var(--text-tertiary)', fontSize: '0.75rem', margin: 0 }}>
                  {entry.reason_not_measured}
                </p>
              </div>
              <div>
                <StateBadge
                  state={entry.state === 'not_measured' ? 'NOT_MEASURED' : 'INCONCLUSIVE'}
                  title={entry.limitation}
                />
                <CoverageMeter entry={entry} />
              </div>
            </div>
          ))}
        </Panel>
      </section>
    </>
  );
}
