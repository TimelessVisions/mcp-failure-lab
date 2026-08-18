/**
 * A three-minute walkthrough, driven by keyboard, offline, and honest.
 *
 * Guided mode is a reading order over the same dataset, not a second dataset. It
 * selects real examples at run time — an actual stable normative result, an
 * actual behavioural divergence — and when the evidence contains no such example
 * the stage says so rather than inventing one. A demo that showed a divergence
 * that was not in the data would be the exact failure this project exists to
 * make impossible elsewhere.
 *
 * There are two scripts. The private one walks real measurements of real
 * implementations. The public-safe one walks the same interface over synthetic
 * evidence and says so on every stage that shows a result, because a
 * demonstration that looks like a finding is a finding as far as a viewer is
 * concerned. Neither script may show what the other is for.
 *
 * Every stage that is supposed to exhibit a particular kind of evidence carries
 * the name of that requirement and whether it resolved. That is what lets a test
 * assert the walkthrough is complete without the test needing to know which
 * examples this particular execution happened to contain.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import {
  Field,
  FieldList,
  Panel,
  SampleStrip,
  ScrollRegion,
  SecurityDimension,
  Stat,
  StateBadge,
  sampleStripLabel,
} from '@/components/primitives';
import { formatMs, transportLabel } from '@/data/selectors';
import type { MatrixCell, TimingCard, ViewerDataset } from '@/data/types';

/**
 * The evidence a complete walkthrough is required to exhibit.
 *
 * Named here rather than in a test, so the requirement and the stage that
 * satisfies it cannot drift apart: a stage declares which one it covers, and
 * anything unclaimed shows up as a gap.
 */
export const GUIDED_REQUIREMENTS = [
  'stable-pass',
  'stable-spec-violation',
  'behavioural-divergence',
  'security-relevant-dimension',
  'not-applicable-with-reason',
  'not-measured-metric',
  'complete-result-signature',
  'containment-record',
  'minimised-reproduction',
  'timing-card-all-samples',
] as const;

export type GuidedRequirement = (typeof GUIDED_REQUIREMENTS)[number];

/** Whether a stage found the evidence it exists to show. */
export type StageEvidence = 'present' | 'absent';

export interface Stage {
  id: string;
  title: string;
  body: React.ReactNode;
  /** The required evidence this stage covers, or null for narrative stages. */
  requirement: GuidedRequirement | null;
  /**
   * `absent` means the dataset contained no such example and the stage says so.
   * It is a legitimate outcome — never a reason to substitute a different
   * example, and never a reason to drop the stage.
   */
  evidence: StageEvidence;
}

function findCell(
  dataset: ViewerDataset,
  predicate: (cell: MatrixCell) => boolean,
): MatrixCell | undefined {
  return dataset.matrix.find(predicate);
}

function targetName(dataset: ViewerDataset, key: string): string {
  return dataset.targets.find((entry) => entry.target_key === key)?.display_name ?? key;
}

function scenarioFor(dataset: ViewerDataset, id: string) {
  return dataset.scenarios.find((entry) => entry.scenario_id === id);
}

/**
 * The evidence-absent state, rendered rather than skipped.
 *
 * Marked in the DOM so a test can tell "this stage found nothing and said so"
 * apart from "this stage rendered nothing at all", which look identical to a
 * reader skimming and are opposite outcomes.
 */
function Absent({ children }: { children: React.ReactNode }) {
  return (
    <p className="callout callout--absent" data-evidence="absent">
      <strong>No such evidence in this execution.</strong> {children}
    </p>
  );
}

/** A timing card with every sample behind it, warm-ups included. */
function TimingEvidence({ card }: { card: TimingCard }) {
  return (
    <>
      <p>
        <strong>{card.name}</strong> — median {formatMs(card.median_ms)} over{' '}
        {card.sample_count} measured samples, {card.warmup_count} warm-up excluded, range{' '}
        {formatMs(card.min_ms)} to {formatMs(card.max_ms)}.
      </p>
      <SampleStrip card={card} />
      <p className="mono" style={{ fontSize: '0.75rem', color: 'var(--text-tertiary)' }}>
        {sampleStripLabel(card)}
      </p>
      <Panel quiet>
        <FieldList>
          <Field
            label="Warm-up samples (excluded)"
            value={card.warmup_samples_ms.map((value) => formatMs(value)).join(' · ')}
            absentNote="none recorded"
          />
          <Field
            label="Measured samples"
            value={card.samples_ms.map((value) => formatMs(value)).join(' · ')}
            absentNote="none recorded"
          />
        </FieldList>
      </Panel>
      <p className="callout">
        Every sample is shown, not a summary of them. A median with the samples withheld cannot
        be checked for the one thing that would invalidate it — a distribution that is not a
        distribution.
      </p>
    </>
  );
}

/**
 * The private script: real implementations, real measurements, real gaps.
 */
export function buildStages(dataset: ViewerDataset): Stage[] {
  const stablePass = findCell(
    dataset,
    (cell) => cell.state === 'PASS' && cell.signature_stable === true,
  );
  const normative = findCell(
    dataset,
    (cell) => cell.state === 'SPEC_VIOLATION' && cell.signature_stable === true,
  );
  const divergence = findCell(dataset, (cell) => cell.state === 'BEHAVIORAL_DIVERGENCE');
  const security = findCell(dataset, (cell) => cell.security_relevant);
  const notApplicable = findCell(dataset, (cell) => cell.state === 'NOT_APPLICABLE');
  const signatureCell = normative ?? divergence ?? stablePass ?? dataset.matrix[0];
  const signatures = signatureCell
    ? dataset.result_signatures.filter(
        (record) =>
          record.target_key === signatureCell.target_key &&
          record.scenario_id === signatureCell.scenario_id,
      )
    : [];
  const signature = signatures[0];
  const containment = dataset.containment[0];
  const reproduction = dataset.reproduction[0];
  const measuredTiming = dataset.timings.find(
    (card) => card.state === 'measured' && card.samples_ms.length > 0,
  );
  const unmeasured = dataset.measurement_coverage.entries.filter(
    (entry) => entry.state === 'not_measured',
  );

  return [
    {
      id: 'problem',
      title: 'The problem',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          <p>{dataset.project.problem}</p>
          <p className="callout">{dataset.project.tagline}</p>
        </>
      ),
    },
    {
      id: 'matrix',
      title: `${dataset.overview.languages.length} languages, ${dataset.overview.transports.length} transports`,
      requirement: null,
      evidence: 'present',
      body: (
        <>
          <div className="grid grid--4">
            <Stat label="Implementations" value={dataset.overview.target_count} />
            <Stat label="Languages" value={dataset.overview.languages.join(' · ')} />
            <Stat
              label="Transports"
              value={dataset.overview.transports.map(transportLabel).join(' · ')}
            />
            <Stat label="Scenarios" value={dataset.overview.scenario_count} />
          </div>
          <p>
            Identical hostile scenarios, run against every implementation the profile and
            transport allow, {dataset.overview.correctness_repetitions} times each.
          </p>
        </>
      ),
    },
    {
      id: 'applicability',
      title: 'Why the applicable sets differ',
      requirement: 'not-applicable-with-reason',
      evidence: notApplicable === undefined ? 'absent' : 'present',
      body:
        notApplicable === undefined ? (
          <Absent>
            Every scenario applied to every implementation in this execution, so there is no
            NOT_APPLICABLE example to show. That is unusual and worth saying rather than
            skipping.
          </Absent>
        ) : (
          <>
            <p>
              <span className="mono">{notApplicable.scenario_id}</span> is{' '}
              <StateBadge state="NOT_APPLICABLE" /> against{' '}
              {targetName(dataset, notApplicable.target_key)}.
            </p>
            <Panel quiet>
              <FieldList>
                <Field
                  label="Reason"
                  value={notApplicable.limitations[0]}
                  absentNote="no reason recorded — itself a gap"
                  prose
                />
                <Field
                  label="Transport spoken"
                  value={transportLabel(notApplicable.transport)}
                />
                <Field label="Profile negotiated" value={notApplicable.profile} />
              </FieldList>
            </Panel>
            <p>
              It sits outside every denominator. Counting it as a failure would turn a transport
              choice into a lower score; counting it as a pass would credit the implementation
              for something it never did.
            </p>
            <p className="callout">
              This is why rows are not comparable as wholes, and why there is no score anywhere
              in this viewer.
            </p>
          </>
        ),
    },
    {
      id: 'stable-pass',
      title: 'One stable pass — and what it does not prove',
      requirement: 'stable-pass',
      evidence: stablePass === undefined ? 'absent' : 'present',
      body:
        stablePass === undefined ? (
          <Absent>No scenario passed stably across every repetition in this execution.</Absent>
        ) : (
          <>
            <p>
              {targetName(dataset, stablePass.target_key)} ·{' '}
              <span className="mono">{stablePass.scenario_id}</span> ·{' '}
              <StateBadge state={stablePass.state} />
            </p>
            <p>
              Stable across {stablePass.repetitions} repetitions by complete result-signature
              equality — the same classification, the same assertion identities, the same
              process outcome, the same cleanup.
            </p>
            <p className="callout">
              A pass means this implementation met this dated requirement under this scenario,
              in this environment. It is not a statement about the implementation as a whole,
              and the passes here do not add up to one.
            </p>
          </>
        ),
    },
    {
      id: 'normative',
      title: 'One stable normative result',
      requirement: 'stable-spec-violation',
      evidence: normative === undefined ? 'absent' : 'present',
      body:
        normative === undefined ? (
          <Absent>
            No stable SPEC_VIOLATION exists in this evidence, so there is none to show. An
            absence of findings from this battery is not evidence of correctness — it is an
            absence of findings from this battery.
          </Absent>
        ) : (
          <>
            <p>
              {targetName(dataset, normative.target_key)} ·{' '}
              <span className="mono">{normative.scenario_id}</span> ·{' '}
              <StateBadge state={normative.state} />
            </p>
            <p>
              Stable across {normative.repetitions} repetitions by complete result-signature
              equality, not by a repeated label. The assertion behind it cites a dated
              requirement of the profile this implementation actually negotiated.
            </p>
          </>
        ),
    },
    {
      id: 'divergence',
      title: 'One behavioural divergence — which is not a defect',
      requirement: 'behavioural-divergence',
      evidence: divergence === undefined ? 'absent' : 'present',
      body:
        divergence === undefined ? (
          <Absent>No behavioural divergence appears in this evidence.</Absent>
        ) : (
          <>
            <p>
              {targetName(dataset, divergence.target_key)} ·{' '}
              <span className="mono">{divergence.scenario_id}</span> ·{' '}
              <StateBadge state={divergence.state} />
            </p>
            <p>
              Implementations differ here and no dated requirement decides between them.
              Reporting it as a violation would be asserting a requirement that does not exist —
              which is the single most common way a conformance tool loses its credibility.
            </p>
          </>
        ),
    },
    {
      id: 'security-dimension',
      title: 'Primary outcome against security relevance',
      requirement: 'security-relevant-dimension',
      evidence: security === undefined ? 'absent' : 'present',
      body:
        security === undefined ? (
          <Absent>
            No security-relevant observation appears in this evidence. Scenarios written to
            probe security properties still ran; none of them observed anything
            security-relevant.
          </Absent>
        ) : (
          <>
            <p>
              {targetName(dataset, security.target_key)} ·{' '}
              <span className="mono">{security.scenario_id}</span> ·{' '}
              <StateBadge state={security.state} />
            </p>
            <SecurityDimension
              cell={security}
              scenario={scenarioFor(dataset, security.scenario_id)}
            />
            <p>
              Three separate facts, recorded side by side. A single enum could hold only one of
              them, and choosing any one destroyed the others: the scenario&apos;s purpose, what
              this run observed, and what anybody has actually confirmed.
            </p>
            <p className="callout">
              Security-relevant is not vulnerable. Confirmed private vulnerabilities in this
              execution: {dataset.overview.confirmed_private_vulnerabilities}. No execution path
              in this harness writes that status.
            </p>
          </>
        ),
    },
    {
      id: 'signature',
      title: 'The complete stability contract',
      requirement: 'complete-result-signature',
      evidence: signature === undefined ? 'absent' : 'present',
      body:
        signature === undefined ? (
          <Absent>No result signature was recorded for the example above.</Absent>
        ) : (
          <>
            <p>
              Stability means every one of these matched across repetitions — not the
              classification alone.
            </p>
            <Panel quiet>
              <FieldList>
                <Field label="Classification" value={signature.classification} />
                <Field
                  label="Failed assertions"
                  value={signature.failed_assertion_ids.join(', ')}
                  absentNote="none failed"
                />
                <Field
                  label="Process outcome"
                  value={`exit ${signature.exit_code ?? '—'} · ${
                    signature.killed_by_harness ? 'killed by the harness' : 'exited on its own'
                  }`}
                />
                <Field
                  label="Cleanup"
                  value={signature.cleanup_ok === true ? 'reclaimed' : 'failed'}
                />
                <Field
                  label="Containment"
                  value={signature.containment_outcome}
                  absentNote="none requested"
                />
                <Field label="Digest" value={signature.digest} />
              </FieldList>
            </Panel>
            <p>
              Durations and timestamps are excluded. A signature that moved on those would
              report instability on every run and mean nothing.
            </p>
          </>
        ),
    },
    {
      id: 'containment',
      title: 'Containment evidence',
      requirement: 'containment-record',
      evidence: containment === undefined ? 'absent' : 'present',
      body:
        containment === undefined ? (
          <Absent>This execution recorded no containment evidence.</Absent>
        ) : (
          <>
            <p>
              Requested and observed, never merged. The case worth surfacing is a restriction
              that was asked for and not applied.
            </p>
            <ScrollRegion label="Containment properties, scrollable">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Property</th>
                    <th scope="col">Requested</th>
                    <th scope="col">Observed</th>
                  </tr>
                </thead>
                <tbody>
                  {containment.facts.slice(0, 6).map((fact) => (
                    <tr key={fact.name}>
                      <th scope="row" style={{ fontWeight: 400 }}>
                        {fact.name}
                      </th>
                      <td className="mono">{fact.requested ?? '—'}</td>
                      <td className="mono">{fact.observed ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ScrollRegion>
            <p className="callout">
              A run without usable containment is not evidence about the target. Its samples are
              rejected rather than reported with a caveat.
            </p>
          </>
        ),
    },
    {
      id: 'reproduction',
      title: 'Reproduction and minimisation',
      requirement: 'minimised-reproduction',
      evidence: reproduction === undefined ? 'absent' : 'present',
      body:
        reproduction === undefined ? (
          <Absent>No reproduction package is described by this dataset.</Absent>
        ) : (
          <>
            <p>
              <span className="mono">{reproduction.reference}</span> —{' '}
              {reproduction.minimization_status}
              {reproduction.original_event_count !== null &&
                reproduction.minimized_event_count !== null &&
                `, ${reproduction.original_event_count} → ${reproduction.minimized_event_count} events`}
              .
            </p>
            <p>
              Minimisation preserves the failure signature, not the label. A reduction that
              reached the same classification by a different route is rejected, because it no
              longer demonstrates the defect it came from.
            </p>
          </>
        ),
    },
    {
      id: 'timings',
      title: 'Timing — every sample, and what remains not measured',
      requirement: 'timing-card-all-samples',
      evidence: measuredTiming === undefined ? 'absent' : 'present',
      body: (
        <>
          {measuredTiming === undefined ? (
            <Absent>This execution produced no measured timing.</Absent>
          ) : (
            <TimingEvidence card={measuredTiming} />
          )}
        </>
      ),
    },
    {
      id: 'coverage',
      title: 'Timing metrics not measured',
      requirement: 'not-measured-metric',
      evidence: unmeasured.length === 0 ? 'absent' : 'present',
      body:
        unmeasured.length === 0 ? (
          <Absent>
            Every timing dimension this methodology names was measured in this execution.
          </Absent>
        ) : (
          <>
            <p className="callout callout--absent">
              {unmeasured.length} of the {dataset.measurement_coverage.entries.length}{' '}
              <strong>timing metrics</strong> this methodology names were not measured. This is
              about the timing metrics, not about the scenario results — a different thing that
              wears the same words. Each states why, and none of them is a zero:
            </p>
            <ul className="limitations">
              {unmeasured.slice(0, 5).map((entry) => (
                <li key={entry.metric_id}>
                  <strong>{entry.display_name}</strong> — {entry.reason_not_measured}
                </li>
              ))}
            </ul>
          </>
        ),
    },
    {
      id: 'methodology',
      title: 'Methodology and limitations',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          <p>
            The rules that decide how everything above should be read, ending where a
            presentation usually stops.
          </p>
          <ul className="limitations">
            {dataset.project.not_claimed.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
          <p className="callout">
            {dataset.visibility_mode === 'private'
              ? 'Private full-evidence mode. Owner-authorized local review only; never published, in whole or in part.'
              : 'Public-safe mode. Third-party identities, versions, fingerprints and finding packages are absent, and this document is not published by this checkpoint either.'}
          </p>
        </>
      ),
    },
  ];
}

/**
 * The public-safe script: the same interface and methodology, no claim about anyone.
 *
 * Deliberately not the private script with the names removed. This walkthrough
 * is about how the evidence is structured and what the tool refuses to say —
 * the applicability rule, the separation of outcome from security, the coverage
 * ledger — demonstrated on data that was invented for the purpose. Every stage
 * that shows a result restates that, because a stage screenshotted on its own
 * loses whatever the first slide said.
 */
export function buildPublicStages(dataset: ViewerDataset): Stage[] {
  const example = dataset.matrix.find((cell) => cell.state === 'SPEC_VIOLATION');
  const notApplicable = dataset.matrix.find((cell) => cell.state === 'NOT_APPLICABLE');
  const timing = dataset.timings.find(
    (card) => card.state === 'measured' && card.samples_ms.length > 0,
  );
  const unmeasured = dataset.measurement_coverage.entries.filter(
    (entry) => entry.state === 'not_measured',
  );
  const synthetic = dataset.provenance.evidence_kind === 'synthetic';

  const note = (
    <p className="callout callout--absent" data-synthetic-note="true">
      Synthetic evidence. The implementations below do not exist and the numbers were invented
      for this demonstration. No third-party benchmark result is shown.
    </p>
  );

  return [
    {
      id: 'public-what-this-is',
      title: 'What this demonstration is',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          <p className="callout" data-synthetic-note="true">
            <strong>{dataset.provenance.eyebrow}.</strong> {dataset.provenance.disclaimer}
          </p>
          <p>{dataset.provenance.evidence_note}</p>
          <p>
            What is real here is the method: the schema, the scenario library, the applicability
            rules and everything this tool refuses to claim. {dataset.provenance.schema_lineage}
          </p>
          {!synthetic && (
            <p className="callout callout--absent">
              This build was produced by redacting real measurements rather than from the
              synthetic fixture. {dataset.provenance.benchmark_identity_note}
            </p>
          )}
        </>
      ),
    },
    {
      id: 'public-problem',
      title: 'The problem being measured',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          <p>{dataset.project.problem}</p>
          <p className="callout">{dataset.project.tagline}</p>
        </>
      ),
    },
    {
      id: 'public-shape',
      title: 'The shape of the measurement',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          {note}
          <div className="grid grid--4">
            <Stat label="Implementations" value={dataset.overview.target_count} />
            <Stat label="Languages" value={dataset.overview.languages.join(' · ')} />
            <Stat
              label="Transports"
              value={dataset.overview.transports.map(transportLabel).join(' · ')}
            />
            <Stat label="Scenarios" value={dataset.overview.scenario_count} />
          </div>
          <p>
            The scenario library and the repetition policy are the real ones. Which invented
            implementation lands where is a fixture, chosen deterministically so this
            demonstration is the same every time it is run.
          </p>
        </>
      ),
    },
    {
      id: 'public-applicability',
      title: 'Applicability — the rule that carries the project',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          {note}
          {notApplicable === undefined ? (
            <p>
              Every scenario applied to every implementation in this fixture, so there is no
              NOT_APPLICABLE cell to point at here.
            </p>
          ) : (
            <p>
              <span className="mono">{notApplicable.scenario_id}</span> is{' '}
              <StateBadge state="NOT_APPLICABLE" /> for one of these implementations: it speaks{' '}
              {transportLabel(notApplicable.transport)} and negotiated {notApplicable.profile}.
            </p>
          )}
          <p>
            A requirement dated after the profile a server negotiated is never asserted against
            it. The result is NOT_APPLICABLE with a reason — never a pass, never a failure. This
            is the rule that decides whether a conformance tool can be trusted at all, and it is
            the same rule in the private benchmark.
          </p>
        </>
      ),
    },
    {
      id: 'public-outcome-vs-security',
      title: 'Primary outcome and security relevance are different fields',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          {note}
          {example !== undefined && (
            <p>
              <span className="mono">{example.scenario_id}</span> ·{' '}
              <StateBadge state={example.state} />
            </p>
          )}
          <p>
            A result can be a normative failure and security-relevant at once, and a scenario
            can be written to probe a security property without anything being observed. Those
            are three separate records — purpose, observation, confirmation — and no execution
            path in this harness writes the third.
          </p>
          <p className="callout">
            Nothing in this demonstration is a security finding about anyone. The rows are
            invented.
          </p>
        </>
      ),
    },
    {
      id: 'public-signature',
      title: 'Stability is signature equality, not a repeated label',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          {note}
          <p>
            A result counts as stable when the <em>complete</em> signature matched across
            repetitions: classification, the identities of every failing assertion, process
            outcome, cleanup and containment. Durations and timestamps are excluded, because a
            signature that moved on those would report instability on every run.
          </p>
          <p>
            Two runs that both said &ldquo;failed&rdquo; for different reasons are not a stable
            result, and this is the mechanism that notices.
          </p>
        </>
      ),
    },
    {
      id: 'public-timings',
      title: 'Timings show every sample',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          {note}
          {timing === undefined ? (
            <p className="callout callout--absent" data-evidence="absent">
              This fixture produced no measured timing card.
            </p>
          ) : (
            <TimingEvidence card={timing} />
          )}
        </>
      ),
    },
    {
      id: 'public-coverage',
      title: 'What was never measured is listed, not omitted',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          <p>
            {unmeasured.length} of the {dataset.measurement_coverage.entries.length}{' '}
            <strong>timing metrics</strong> the methodology names are not measured, each with a
            recorded reason. A metric absent from a report is one a reader cannot tell was never
            taken — so none of them is absent, and none is rendered as a zero.
          </p>
          <p className="callout">
            This is a coverage ledger for timing metrics. Scenario results have their own
            not-measured state, which is a different statement.
          </p>
        </>
      ),
    },
    {
      id: 'public-refusals',
      title: 'What this tool refuses to say',
      requirement: null,
      evidence: 'present',
      body: (
        <>
          <ul className="limitations">
            {dataset.project.not_claimed.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
          <p className="callout" data-synthetic-note="true">
            {dataset.provenance.disclaimer} The private benchmark that shares this schema is not
            represented here in any form.
          </p>
        </>
      ),
    },
  ];
}

/** The script this dataset gets. Never a choice a build flag makes. */
export function stagesFor(dataset: ViewerDataset): Stage[] {
  return dataset.visibility_mode === 'private'
    ? buildStages(dataset)
    : buildPublicStages(dataset);
}

/**
 * Which required evidence the walkthrough covered, and how.
 *
 * Used by the tests and by CI. A requirement that no stage claims is a hole in
 * the walkthrough; one that resolved to `absent` is a hole in the *evidence*,
 * which is a fact about the execution and perfectly acceptable to report.
 */
export function auditStages(stages: Stage[]): {
  requirement: GuidedRequirement;
  covered: boolean;
  evidence: StageEvidence | null;
}[] {
  return GUIDED_REQUIREMENTS.map((requirement) => {
    const stage = stages.find((candidate) => candidate.requirement === requirement);
    return {
      requirement,
      covered: stage !== undefined,
      evidence: stage?.evidence ?? null,
    };
  });
}

export function GuidedMode({
  dataset,
  onExit,
}: {
  dataset: ViewerDataset;
  onExit: () => void;
}) {
  const stages = useMemo(() => stagesFor(dataset), [dataset]);
  const [index, setIndex] = useState(0);
  const current = stages[index];

  const go = useCallback(
    (delta: number) => {
      setIndex((previous) => Math.min(stages.length - 1, Math.max(0, previous + delta)));
    },
    [stages.length],
  );

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onExit();
      else if (event.key === 'ArrowRight' || event.key === 'PageDown') go(1);
      else if (event.key === 'ArrowLeft' || event.key === 'PageUp') go(-1);
      else if (event.key === 'Home') setIndex(0);
      else if (event.key === 'End') setIndex(stages.length - 1);
      else return;
      event.preventDefault();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [go, onExit, stages.length]);

  if (current === undefined) return null;

  return (
    <div
      className="guided"
      role="dialog"
      aria-modal="true"
      aria-label="Guided walkthrough"
      data-mode={dataset.visibility_mode}
    >
      <div className="guided__bar">
        <div>
          <p className="eyebrow" style={{ margin: 0 }}>
            Guided walkthrough · stage {index + 1} of {stages.length}
          </p>
          <strong>{current.title}</strong>
        </div>
        <div className="toolbar" style={{ margin: 0 }}>
          <button
            type="button"
            className="button"
            onClick={() => go(-1)}
            disabled={index === 0}
          >
            ← Back
          </button>
          <button
            type="button"
            className="button button--primary"
            onClick={() => go(1)}
            disabled={index === stages.length - 1}
          >
            Next →
          </button>
          <button type="button" className="button" onClick={onExit}>
            Exit (Esc)
          </button>
        </div>
      </div>

      <div className="guided__body">
        <div
          className="guided__stage"
          key={current.id}
          data-stage-id={current.id}
          data-requirement={current.requirement ?? undefined}
          data-evidence={current.evidence}
        >
          <h2 className="view-title" tabIndex={-1} autoFocus>
            {current.title}
          </h2>
          <div style={{ marginTop: 'var(--space-4)' }}>{current.body}</div>
        </div>
      </div>

      <div className="guided__progress" aria-hidden="true">
        {stages.map((stage, position) => (
          <span
            key={stage.id}
            className={
              position === index
                ? 'guided__tick guided__tick--current'
                : position < index
                  ? 'guided__tick guided__tick--done'
                  : 'guided__tick'
            }
          />
        ))}
      </div>
    </div>
  );
}
