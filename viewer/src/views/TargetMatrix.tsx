/**
 * Implementations down, scenarios across, and no row that wins.
 *
 * Two decisions carry this screen. Rows are ordered alphabetically and never by
 * outcome, because an ordering *is* a ranking however it is captioned. And every
 * cell opens an evidence panel carrying its own denominator, repetitions,
 * signature stability, profile, transport and limitations — a cell that showed
 * only a colour would be inviting exactly the aggregate reading the whole
 * project refuses.
 */

import { useMemo, useState } from 'react';

import {
  Field,
  FieldList,
  Limitations,
  ScrollRegion,
  SecurityBadge,
  SecurityDimension,
  StateBadge,
  ViewHeader,
} from '@/components/primitives';
import { hrefFor } from '@/app/routes';
import {
  STATE_MEANING,
  describeDenominator,
  indexMatrix,
  stateFamily,
  transportLabel,
} from '@/data/selectors';
import type { MatrixCell, ViewerDataset } from '@/data/types';

export function TargetMatrix({
  dataset,
  headingRef,
}: {
  dataset: ViewerDataset;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const [selected, setSelected] = useState<MatrixCell | null>(null);
  const index = useMemo(() => indexMatrix(dataset), [dataset]);

  const targets = useMemo(
    () => [...dataset.targets].sort((a, b) => a.display_name.localeCompare(b.display_name)),
    [dataset.targets],
  );
  const scenarios = useMemo(
    () =>
      [...dataset.scenarios].sort(
        (a, b) =>
          a.category.localeCompare(b.category) || a.scenario_id.localeCompare(b.scenario_id),
      ),
    [dataset.scenarios],
  );

  return (
    <>
      <ViewHeader
        eyebrow="Target matrix"
        title="Every implementation against every scenario"
        lede={
          <p>
            Rows are alphabetical. They are <strong>not</strong> a ranking and the columns do
            not combine into a score: each implementation has a different applicable set,
            because applicability depends on the transport it speaks and the surface it
            advertises. More green does not mean better — it frequently means narrower.
          </p>
        }
        headingRef={headingRef}
      />

      {/*
       * Two presentations of one dataset, chosen by CSS rather than by a
       * viewport guess in JavaScript. The table is the right shape for
       * comparing three implementations side by side and the wrong shape for a
       * 390px screen, where it became a horizontally scrolling box whose
       * implementation columns started off-screen — a reader had to already
       * know they were there.
       */}
      <div className="only-narrow">
        <MobileMatrix dataset={dataset} targets={targets} scenarios={scenarios} index={index} />
      </div>

      <div className="only-wide">
        <ScrollRegion label="Target matrix, scrollable">
          <table>
            <caption>
              {targets.length} implementations × {scenarios.length} scenarios. Select a cell for
              its denominator, repetitions, signature stability and limitations.
            </caption>
            <thead>
              <tr>
                <th scope="col">Scenario</th>
                <th scope="col">Category</th>
                {targets.map((target) => (
                  <th key={target.target_key} scope="col">
                    <a href={hrefFor({ name: 'target', targetKey: target.target_key })}>
                      {target.display_name}
                    </a>
                    <div style={{ color: 'var(--text-quiet)', fontWeight: 400 }}>
                      {target.language} · {target.transport}
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {scenarios.map((scenario) => (
                <tr key={scenario.scenario_id}>
                  <th scope="row" style={{ fontWeight: 400 }}>
                    <a
                      className="mono"
                      href={hrefFor({ name: 'scenario', scenarioId: scenario.scenario_id })}
                    >
                      {scenario.scenario_id}
                    </a>
                  </th>
                  <td style={{ color: 'var(--text-quiet)', fontSize: '0.6875rem' }}>
                    {scenario.category}
                  </td>
                  {targets.map((target) => {
                    const cell = index.get(`${target.target_key}::${scenario.scenario_id}`);
                    if (cell === undefined) {
                      return (
                        <td key={target.target_key}>
                          <span className="badge badge--absent">NOT_MEASURED</span>
                        </td>
                      );
                    }
                    const isSelected =
                      selected?.target_key === cell.target_key &&
                      selected.scenario_id === cell.scenario_id;
                    return (
                      <td key={target.target_key}>
                        <button
                          type="button"
                          className="cell-button"
                          data-family={stateFamily(cell.state)}
                          data-state={cell.state}
                          aria-pressed={isSelected}
                          aria-label={`${target.display_name}, ${scenario.scenario_id}: ${cell.state}. ${describeDenominator(
                            cell.applicable ? 1 : 0,
                            cell.applicable_denominator,
                          )}`}
                          onClick={() => setSelected(isSelected ? null : cell)}
                        >
                          {cell.state}
                          {cell.security_relevant && ' ·sec'}
                        </button>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollRegion>

        {selected !== null && (
          <section className="section" aria-live="polite">
            <CellEvidence cell={selected} dataset={dataset} />
          </section>
        )}
      </div>
    </>
  );
}

/**
 * The same comparison, one scenario at a time, stacked.
 *
 * A phone cannot show three implementation columns beside a scenario
 * identifier, and the honest response is a different arrangement rather than a
 * smaller one. The scenario is chosen once, at the top, and every
 * implementation's result for it appears below in full — classification,
 * denominator, repetitions, stability, profile, transport and limitations —
 * so nothing requires horizontal movement and nothing is hidden behind a
 * gesture the reader has to guess at.
 */
function MobileMatrix({
  dataset,
  targets,
  scenarios,
  index,
}: {
  dataset: ViewerDataset;
  targets: ViewerDataset['targets'];
  scenarios: ViewerDataset['scenarios'];
  index: Map<string, MatrixCell>;
}) {
  const [scenarioId, setScenarioId] = useState<string>(scenarios[0]?.scenario_id ?? '');
  const scenario = scenarios.find((entry) => entry.scenario_id === scenarioId) ?? scenarios[0];
  const categories = [...new Set(scenarios.map((entry) => entry.category))];

  if (scenario === undefined) {
    return <p className="callout callout--absent">This dataset describes no scenarios.</p>;
  }

  return (
    <div className="stacked-matrix">
      <div className="stacked-matrix__picker">
        <label className="stacked-matrix__label" htmlFor="stacked-matrix-scenario">
          Scenario — {scenarios.length} in this execution
        </label>
        <select
          id="stacked-matrix-scenario"
          className="select"
          value={scenario.scenario_id}
          onChange={(event) => setScenarioId(event.target.value)}
        >
          {categories.map((category) => (
            <optgroup key={category} label={category}>
              {scenarios
                .filter((entry) => entry.category === category)
                .map((entry) => (
                  <option key={entry.scenario_id} value={entry.scenario_id}>
                    {entry.scenario_id}
                  </option>
                ))}
            </optgroup>
          ))}
        </select>
      </div>

      <div className="stacked-matrix__scenario">
        <p className="eyebrow" style={{ margin: 0 }}>
          {scenario.category}
        </p>
        <p className="mono" style={{ margin: 'var(--space-1) 0 0' }}>
          <a href={hrefFor({ name: 'scenario', scenarioId: scenario.scenario_id })}>
            {scenario.scenario_id}
          </a>
        </p>
        {scenario.purpose !== null && (
          <p className="stacked-matrix__purpose">{scenario.purpose}</p>
        )}
      </div>

      <p className="stacked-matrix__count">
        {targets.length} implementations, each shown in full below. Results are not comparable
        as a total: every implementation has its own applicable set.
      </p>

      {targets.map((target) => {
        const cell = index.get(`${target.target_key}::${scenario.scenario_id}`);
        return (
          <article className="stacked-cell" key={target.target_key}>
            <header className="stacked-cell__header">
              <a href={hrefFor({ name: 'target', targetKey: target.target_key })}>
                {target.display_name}
              </a>
              <span className="stacked-cell__meta">
                {target.language} · {transportLabel(target.transport)}
              </span>
            </header>

            {cell === undefined ? (
              <p className="callout callout--absent">
                <span className="badge badge--absent">NOT_MEASURED</span> — no scenario result
                was recorded for this implementation.
              </p>
            ) : (
              <>
                <p className="stacked-cell__state">
                  <StateBadge state={cell.state} />
                  {cell.security_relevant && (
                    <SecurityBadge status={cell.vulnerability_status} />
                  )}
                </p>
                <p className="stacked-cell__meaning">{STATE_MEANING[cell.state]}</p>
                <FieldList>
                  <Field
                    label="Applicable denominator"
                    value={describeDenominator(
                      cell.applicable ? 1 : 0,
                      cell.applicable_denominator,
                    )}
                  />
                  <Field
                    label="Repetitions"
                    value={`${cell.valid_repetitions} valid of ${cell.repetitions}`}
                  />
                  <Field
                    label="Signature stability"
                    value={
                      cell.signature_stable === null
                        ? 'too few valid repetitions to compare'
                        : cell.signature_stable
                          ? `stable — ${cell.signature_digests.length} identical signature`
                          : `unstable — ${cell.signature_digests.length} distinct signatures`
                    }
                  />
                  <Field label="Negotiated profile" value={cell.profile} />
                  <Field label="Transport" value={transportLabel(cell.transport)} />
                </FieldList>
                <SecurityDimension cell={cell} scenario={scenario} />
                <Limitations items={cell.limitations} />
                <p style={{ marginTop: 'var(--space-3)' }}>
                  <a
                    href={hrefFor({
                      name: 'signature',
                      targetKey: cell.target_key,
                      scenarioId: cell.scenario_id,
                    })}
                  >
                    Complete result signature →
                  </a>
                </p>
              </>
            )}
          </article>
        );
      })}

      <p className="stacked-matrix__note">
        This is the same evidence as the table shown on wider screens, arranged one scenario at
        a time. Nothing is omitted here — {dataset.matrix.length} cells are reachable through
        the selector above.
      </p>
    </div>
  );
}

export function CellEvidence({ cell, dataset }: { cell: MatrixCell; dataset: ViewerDataset }) {
  const target = dataset.targets.find((entry) => entry.target_key === cell.target_key);
  const scenario = dataset.scenarios.find((entry) => entry.scenario_id === cell.scenario_id);

  return (
    <div className="evidence-panel">
      <h2 className="section__title">
        {target?.display_name ?? cell.target_key} ·{' '}
        <span className="mono">{cell.scenario_id}</span>
      </h2>
      <p className="callout">{STATE_MEANING[cell.state]}</p>

      <FieldList>
        <Field label="Primary outcome" value={<StateBadge state={cell.state} />} />
        <Field label="Normative assertion evaluated" value={cell.normative ? 'yes' : 'no'} />
        <Field
          label="Applicable denominator"
          value={describeDenominator(cell.applicable ? 1 : 0, cell.applicable_denominator)}
        />
        <Field
          label="Repetitions"
          value={`${cell.valid_repetitions} valid of ${cell.repetitions}`}
        />
        <Field
          label="Signature stability"
          value={
            cell.signature_stable === null
              ? 'too few valid repetitions to compare'
              : cell.signature_stable
                ? `stable — ${cell.signature_digests.length} identical signature`
                : `unstable — ${cell.signature_digests.length} distinct signatures`
          }
        />
        <Field label="Negotiated profile" value={cell.profile} />
        <Field label="Transport" value={cell.transport} />
        <Field
          label="Scenario purpose"
          value={scenario?.purpose ?? null}
          prose
          absentNote="the scenario library carries no purpose for this id"
        />
      </FieldList>

      <h3 className="section__title" style={{ marginTop: 'var(--space-5)' }}>
        Security
      </h3>
      <SecurityDimension cell={cell} scenario={scenario} />

      <p style={{ marginTop: 'var(--space-4)' }}>
        <a
          href={hrefFor({
            name: 'signature',
            targetKey: cell.target_key,
            scenarioId: cell.scenario_id,
          })}
        >
          Open the complete result signature →
        </a>
      </p>

      <Limitations items={cell.limitations} />
    </div>
  );
}
