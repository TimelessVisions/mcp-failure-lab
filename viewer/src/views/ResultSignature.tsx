/**
 * The stability contract, in full, with repetitions side by side.
 *
 * "Stable across repetitions" was the claim this screen exists to make
 * checkable. Two runs can share a classification while different assertions
 * failed, or while one was killed by the harness and the other exited on its
 * own, or against two different capability surfaces. Every one of those is a
 * different result wearing the same label, so the whole signature is displayed
 * and the repetitions are shown adjacent rather than summarised.
 *
 * Durations and timestamps are deliberately absent: a signature that moved on
 * them would report instability on every run and mean nothing.
 */

import { useMemo } from 'react';

import { Field, FieldList, Panel, StateBadge, ViewHeader } from '@/components/primitives';
import { hrefFor } from '@/app/routes';
import { signaturesFor } from '@/data/selectors';
import type { CellState, SignatureRecord, ViewerDataset } from '@/data/types';

function asCellState(classification: string): CellState {
  return classification as CellState;
}

export function ResultSignatureView({
  dataset,
  targetKey,
  scenarioId,
  headingRef,
}: {
  dataset: ViewerDataset;
  targetKey?: string | undefined;
  scenarioId?: string | undefined;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const pairs = useMemo(() => {
    const seen = new Map<string, { targetKey: string; scenarioId: string }>();
    for (const record of dataset.result_signatures) {
      seen.set(`${record.target_key}::${record.scenario_id}`, {
        targetKey: record.target_key,
        scenarioId: record.scenario_id,
      });
    }
    return [...seen.values()].sort(
      (a, b) =>
        a.targetKey.localeCompare(b.targetKey) || a.scenarioId.localeCompare(b.scenarioId),
    );
  }, [dataset.result_signatures]);

  const selected =
    targetKey !== undefined && scenarioId !== undefined ? { targetKey, scenarioId } : pairs[0];

  if (selected === undefined) {
    return (
      <ViewHeader
        eyebrow="Result signature"
        title="This execution recorded no result signatures"
        lede={
          <p>
            Without them a repeated classification could not be shown to be a repeated result,
            and every sample would have been rejected for it.
          </p>
        }
        headingRef={headingRef}
      />
    );
  }

  const records = signaturesFor(dataset, selected);
  const digests = [...new Set(records.map((record) => record.digest))];
  const target = dataset.targets.find((entry) => entry.target_key === selected.targetKey);

  return (
    <>
      <ViewHeader
        eyebrow="Result signature"
        title="What has to match before two runs are the same result"
        lede={
          <p>
            Stability is equality of this whole structure, not a repeated label. Durations,
            timestamps and run identifiers are excluded on purpose — a signature that moved on
            those would report instability on every run and mean nothing.
          </p>
        }
        headingRef={headingRef}
      />

      <div className="toolbar">
        <label htmlFor="signature-pair" className="field__label">
          Result
        </label>
        <select
          id="signature-pair"
          className="button"
          value={`${selected.targetKey}::${selected.scenarioId}`}
          onChange={(event) => {
            const [nextTarget, nextScenario] = event.target.value.split('::');
            if (nextTarget !== undefined && nextScenario !== undefined) {
              window.location.hash = hrefFor({
                name: 'signature',
                targetKey: nextTarget,
                scenarioId: nextScenario,
              });
            }
          }}
        >
          {pairs.map((pair) => (
            <option
              key={`${pair.targetKey}::${pair.scenarioId}`}
              value={`${pair.targetKey}::${pair.scenarioId}`}
            >
              {pair.targetKey} — {pair.scenarioId}
            </option>
          ))}
        </select>
      </div>

      <p className="callout">
        {target?.display_name ?? selected.targetKey} ·{' '}
        <span className="mono">{selected.scenarioId}</span> ·{' '}
        {records.length === 0
          ? 'no signature was recorded for this pair'
          : digests.length === 1
            ? `stable across ${records.length} repetitions — one identical signature`
            : `unstable — ${digests.length} distinct signatures across ${records.length} repetitions`}
      </p>

      <div className="grid grid--2">
        {records.map((record) => (
          <SignaturePanel key={record.repetition} record={record} digests={digests} />
        ))}
      </div>

      {records.length > 1 && digests.length > 1 && (
        <section className="section">
          <Panel>
            <h2 className="section__title">Why the repetitions are not the same result</h2>
            <p>
              The classifications above may agree. The signatures do not, and the difference is
              what the comparison exists to catch: the same label can cover different failing
              assertions, a harness kill against a target crash, or two different capability
              surfaces.
            </p>
            <ul className="limitations">
              {digests.map((digest, index) => (
                <li key={digest} className="mono">
                  signature {index + 1}: {digest}
                </li>
              ))}
            </ul>
          </Panel>
        </section>
      )}
    </>
  );
}

function SignaturePanel({ record, digests }: { record: SignatureRecord; digests: string[] }) {
  const variant = digests.indexOf(record.digest) + 1;
  return (
    <Panel title={`Repetition ${record.repetition} · signature ${variant}`}>
      <FieldList>
        <Field
          label="Primary classification"
          value={<StateBadge state={asCellState(record.classification)} />}
        />
        <Field
          label="Failed assertions"
          value={record.failed_assertion_ids.join(', ')}
          absentNote="none failed"
        />
        <Field
          label="Assertion-level classifications"
          value={
            <ul className="inline-list">
              {record.assertions.map((assertion) => (
                <li key={assertion.assertion_id} className="tag">
                  {assertion.assertion_id} · {assertion.status}
                  {assertion.classification !== null && ` · ${assertion.classification}`}
                  {assertion.security_relevant && ' · sec'}
                </li>
              ))}
            </ul>
          }
          absentNote="no assertion was evaluated"
        />
        <Field
          label="Timeout cause"
          value={record.timeout_cause}
          absentNote="no deadline expired"
        />
        <Field
          label="Process outcome"
          value={`exit ${record.exit_code ?? '—'} · signal ${record.signal ?? '—'} · ${
            record.killed_by_harness ? 'killed by the harness' : 'exited on its own'
          }`}
        />
        <Field label="Cleanup" value={record.cleanup_ok ? 'reclaimed' : 'failed'} />
        <Field
          label="Containment"
          value={`${record.containment_outcome ?? 'none requested'} · ${
            record.containment_usable ? 'usable as evidence' : 'not usable as evidence'
          }`}
        />
        <Field
          label="Target fingerprint"
          value={record.target_fingerprint}
          absentNote="withheld in public-safe mode"
        />
        <Field label="Scenario fingerprint" value={record.scenario_fingerprint} />
        <Field
          label="Capability fingerprint"
          value={record.capability_fingerprint}
          absentNote="withheld in public-safe mode"
        />
        <Field
          label="Security dimension"
          value={
            record.security_relevant
              ? `${record.security_category ?? 'uncategorised'} · ${record.vulnerability_status}`
              : 'not security-relevant'
          }
        />
        <Field label="Normative assertion evaluated" value={record.normative ? 'yes' : 'no'} />
        <Field
          label="First meaningful difference"
          value={record.first_meaningful_difference}
          absentNote="this result did not come from a differential comparison"
        />
        <Field label="Signature digest" value={record.digest} />
        <Field label="Signature scheme" value={`v${record.signature_version}`} />
      </FieldList>
    </Panel>
  );
}
