/**
 * One scenario: what it asks, what backs it, and where it could not apply.
 *
 * The normative citation is displayed at the top rather than in a footnote. An
 * assertion quoting a dated requirement and an assertion expressing this
 * project's opinion look identical in a results table, and the difference
 * between them decides whether a non-pass is a defect or a divergence.
 */

import {
  Field,
  FieldList,
  Limitations,
  Panel,
  ScrollRegion,
  StateBadge,
  ViewHeader,
} from '@/components/primitives';
import { hrefFor } from '@/app/routes';
import { cellsForScenario, describeDenominator } from '@/data/selectors';
import type { ViewerDataset } from '@/data/types';

export function ScenarioDetail({
  dataset,
  scenarioId,
  headingRef,
}: {
  dataset: ViewerDataset;
  scenarioId: string;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const scenario = dataset.scenarios.find((entry) => entry.scenario_id === scenarioId);
  if (scenario === undefined) {
    return (
      <ViewHeader
        eyebrow="Scenario"
        title="No such scenario in this dataset"
        lede={
          <p>
            <span className="mono">{scenarioId}</span> is not pinned by this benchmark.{' '}
            <a href={hrefFor({ name: 'matrix' })}>Back to the matrix</a>.
          </p>
        }
        headingRef={headingRef}
      />
    );
  }

  const cells = cellsForScenario(dataset, scenarioId);

  return (
    <>
      <ViewHeader
        eyebrow={`Scenario · ${scenario.category}`}
        title={scenario.title}
        lede={<p className="mono">{scenario.scenario_id}</p>}
        headingRef={headingRef}
      />

      <section className="section" aria-labelledby="scenario-purpose">
        <h2 className="section__title" id="scenario-purpose">
          What it asks
        </h2>
        <p>{scenario.purpose}</p>
        <Panel quiet>
          <FieldList>
            <Field label="Expected outcome" value={scenario.expected_outcome} prose />
            <Field label="Nature" value={scenario.nature} />
            <Field label="Severity" value={scenario.severity} />
            <Field
              label="Normative source"
              value={scenario.normative_source}
              prose
              absentNote="none — this scenario asserts no dated requirement, and a non-pass here is a divergence rather than a defect"
            />
            <Field
              label="Rationale"
              value={scenario.rationale}
              prose
              absentNote="no rationale recorded"
            />
            <Field
              label="Security category"
              value={scenario.security_category}
              absentNote="this scenario is not about a security property"
            />
            <Field label="Scenario fingerprint" value={scenario.scenario_fingerprint} />
            <Field label="Version" value={scenario.version} />
          </FieldList>
        </Panel>
      </section>

      <section className="section" aria-labelledby="scenario-applicability">
        <h2 className="section__title" id="scenario-applicability">
          Applicability prerequisites
        </h2>
        <Panel quiet>
          <FieldList>
            <Field
              label="Profiles"
              value={scenario.applicability_profiles.join(', ')}
              absentNote="every profile"
            />
            <Field
              label="Transports"
              value={scenario.applicability_transports.join(', ')}
              absentNote="every transport"
            />
            <Field
              label="Minimum spec revision"
              value={scenario.applicability_min_spec_version}
              absentNote="no lower bound"
            />
            <Field
              label="Maximum spec revision"
              value={scenario.applicability_max_spec_version}
              absentNote="no upper bound"
            />
            <Field
              label="Required tools"
              value={scenario.requires_tools.join(', ')}
              absentNote="none"
            />
            <Field
              label="Required observed transport features"
              value={Object.entries(scenario.requires_transport_features)
                .map(([key, value]) => `${key}=${value}`)
                .join(' · ')}
              absentNote="none"
            />
            <Field
              label="Needs an approved safe invocation"
              value={scenario.requires_safe_tool_invocation ? 'yes' : 'no'}
            />
            <Field
              label="Applicability rests on"
              value={
                scenario.requires_observed_surface
                  ? 'what the target advertised, observed at run time'
                  : 'declared facts alone — profile and transport'
              }
              prose
            />
          </FieldList>
        </Panel>
        <p className="callout" style={{ marginTop: 'var(--space-4)' }}>
          A scenario that could not apply is <strong>NOT_APPLICABLE</strong> with the reason
          recorded. It is never a pass and never a failure, and it sits outside every
          denominator.
        </p>
      </section>

      <section className="section" aria-labelledby="scenario-assertions">
        <h2 className="section__title" id="scenario-assertions">
          Assertions
        </h2>
        <ul className="inline-list">
          {scenario.assertion_ids.map((id) => (
            <li key={id} className="tag">
              {id}
            </li>
          ))}
        </ul>
        {scenario.assertion_ids.length === 0 && (
          <p className="callout callout--absent">This scenario evaluates no named assertion.</p>
        )}
      </section>

      <section className="section" aria-labelledby="scenario-results">
        <h2 className="section__title" id="scenario-results">
          Results by implementation
        </h2>
        <ScrollRegion label="Results by implementation, scrollable">
          <table>
            <caption>
              Official conformance overlap: the official framework's verdict on this scenario is
              recorded in a separate vocabulary and never merged into these classifications.
            </caption>
            <thead>
              <tr>
                <th scope="col">Implementation</th>
                <th scope="col">Outcome</th>
                <th scope="col">Applicable</th>
                <th scope="col">Signature stability</th>
                <th scope="col">Failed assertions</th>
              </tr>
            </thead>
            <tbody>
              {cells.map((cell) => {
                const target = dataset.targets.find(
                  (entry) => entry.target_key === cell.target_key,
                );
                const signature = dataset.result_signatures.find(
                  (record) =>
                    record.target_key === cell.target_key &&
                    record.scenario_id === cell.scenario_id,
                );
                return (
                  <tr key={cell.target_key}>
                    <th scope="row" style={{ fontWeight: 400 }}>
                      <a href={hrefFor({ name: 'target', targetKey: cell.target_key })}>
                        {target?.display_name ?? cell.target_key}
                      </a>
                    </th>
                    <td>
                      <StateBadge state={cell.state} />
                    </td>
                    <td>
                      {describeDenominator(
                        cell.applicable ? 1 : 0,
                        cell.applicable_denominator,
                      )}
                    </td>
                    <td>
                      {cell.signature_stable === null
                        ? 'not comparable'
                        : cell.signature_stable
                          ? 'stable'
                          : `${cell.signature_digests.length} distinct signatures`}
                    </td>
                    <td className="mono">
                      {signature === undefined || signature.failed_assertion_ids.length === 0
                        ? '—'
                        : signature.failed_assertion_ids.join(', ')}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </ScrollRegion>
      </section>

      <Panel quiet>
        <Limitations items={scenario.limitations} />
      </Panel>
    </>
  );
}
