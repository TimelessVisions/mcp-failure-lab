/**
 * Reproduction packages, described by their shape and never by their contents.
 *
 * A minimised reproduction is the most persuasive artifact this project makes
 * and the most dangerous one to display. So this screen shows what a package
 * *is* — status, stability runs, how far minimisation reduced it, whether the
 * failure signature survived — and never what it contains. In public-safe mode
 * the private packages are absent entirely rather than summarised, because a
 * finding's existence is itself disclosure.
 */

import { Field, FieldList, Limitations, Panel, ViewHeader } from '@/components/primitives';
import type { ReproductionSummary, ViewerDataset } from '@/data/types';

function reduction(summary: ReproductionSummary): string | null {
  if (summary.original_event_count === null || summary.minimized_event_count === null) {
    return null;
  }
  return `${summary.original_event_count} → ${summary.minimized_event_count} events`;
}

function bytes(summary: ReproductionSummary): string | null {
  if (summary.original_body_bytes === null || summary.minimized_body_bytes === null) {
    return null;
  }
  return `${summary.original_body_bytes} → ${summary.minimized_body_bytes} bytes`;
}

export function Reproduction({
  dataset,
  headingRef,
}: {
  dataset: ViewerDataset;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const isPrivate = dataset.visibility_mode === 'private';
  return (
    <>
      <ViewHeader
        eyebrow="Reproduction and minimisation"
        title="Failures reduced to something someone else can run"
        lede={
          <p>
            Minimisation preserves the <strong>failure signature</strong> — the classification
            and the identities of the failing assertions — not merely the label. A reduction
            that reached the same label by a different route is rejected, because it no longer
            demonstrates the defect it came from.
          </p>
        }
        headingRef={headingRef}
      />

      {!isPrivate && (
        <p className="callout callout--absent">
          Public-safe mode. Private finding packages are absent from this document entirely
          rather than summarised: the existence of an undisclosed finding is itself disclosure.
          What remains are first-party fixtures whose defects this repository authored on
          purpose.
        </p>
      )}

      {dataset.reproduction.length === 0 && (
        <p className="callout callout--absent">
          No reproduction packages are described by this dataset.
        </p>
      )}

      <div className="grid grid--2">
        {dataset.reproduction.map((summary) => (
          <Panel key={summary.reference} title={summary.reference}>
            <FieldList>
              <Field label="Status" value={summary.status} />
              <Field
                label="Nature"
                value={
                  summary.synthetic
                    ? 'first-party fixture with a deliberate defect'
                    : 'private finding package — contents never displayed'
                }
                prose
              />
              <Field label="Minimisation" value={summary.minimization_status} />
              <Field
                label="Reduction"
                value={reduction(summary)}
                absentNote="no minimisation record"
              />
              <Field
                label="Body size"
                value={bytes(summary)}
                absentNote="no minimisation record"
              />
              <Field
                label="Stability runs"
                value={summary.stability_runs === 0 ? null : summary.stability_runs}
                absentNote="not recorded"
              />
              <Field
                label="Signature preserved"
                value={
                  summary.preserved_signature === null
                    ? null
                    : summary.preserved_signature
                      ? 'yes — classification and failing assertion identities held'
                      : 'no — the reduction is reported as-is rather than as a reduction'
                }
                absentNote="not recorded"
              />
              <Field
                label="Replay requirements"
                value={summary.replay_requirements.join(' ')}
                prose
                absentNote="none recorded"
              />
            </FieldList>
            <Limitations items={summary.limitations} />
          </Panel>
        ))}
      </div>
    </>
  );
}
