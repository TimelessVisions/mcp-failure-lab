/**
 * Requested against observed, in two columns that never merge.
 *
 * A single "network: none" tells you what somebody asked for or what the runtime
 * did, and you cannot tell which. The case worth surfacing is the one where they
 * disagree — a restriction requested and not applied — and a merged value is
 * exactly the rendering that hides it.
 */

import {
  Field,
  FieldList,
  Limitations,
  Panel,
  ScrollRegion,
  ViewHeader,
} from '@/components/primitives';
import { hrefFor } from '@/app/routes';
import type { ContainmentFact, ViewerDataset } from '@/data/types';

function agreementLabel(fact: ContainmentFact): string {
  if (fact.agrees === null) return 'not comparable';
  return fact.agrees ? 'applied' : 'not applied';
}

function agreementFamily(fact: ContainmentFact): string {
  if (fact.agrees === null) return 'absent';
  return fact.agrees ? 'pass' : 'normative';
}

export function Containment({
  dataset,
  headingRef,
}: {
  dataset: ViewerDataset;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  return (
    <>
      <ViewHeader
        eyebrow="Containment"
        title="What was requested, and what the runtime actually did"
        lede={
          <p>
            A run without usable containment is not evidence about the target, and its samples
            are rejected rather than reported with a caveat. Builder-local image identifiers
            differ between storage drivers for identical bits — that is not target drift, and
            cross-environment agreement rests on the portable build inputs instead.
          </p>
        }
        headingRef={headingRef}
      />

      {dataset.containment.length === 0 && (
        <p className="callout callout--absent">
          This execution recorded no containment evidence. Absent evidence, not weak
          containment.
        </p>
      )}

      {dataset.containment.map((evidence) => {
        const target = dataset.targets.find(
          (entry) => entry.target_key === evidence.target_key,
        );
        return (
          <section className="section" key={evidence.target_key}>
            <h2 className="section__title">
              <a href={hrefFor({ name: 'target', targetKey: evidence.target_key })}>
                {target?.display_name ?? evidence.target_key}
              </a>
            </h2>

            <Panel quiet>
              <FieldList>
                <Field label="Execution mode" value={evidence.mode} />
                <Field label="Provenance" value={evidence.provenance} />
                <Field label="Outcome" value={evidence.outcome} />
                <Field
                  label="Usable as evidence"
                  value={evidence.usable ? 'yes' : 'no — samples from it are rejected'}
                />
                <Field
                  label="Image source kind"
                  value={evidence.image_source_kind}
                  absentNote="withheld in public-safe mode"
                />
                <Field
                  label="Portable build identity"
                  value={evidence.portable_build_identity}
                  absentNote="withheld in public-safe mode"
                />
                <Field
                  label="Builder-local image identity"
                  value={evidence.builder_local_image_identity}
                  absentNote="withheld — this builder's name for these bits, portable to nothing"
                />
                <Field
                  label="Unavailable reason"
                  value={evidence.unavailable_reason}
                  prose
                  absentNote="containment was obtained"
                />
              </FieldList>
            </Panel>

            <ScrollRegion label="Requested against observed, scrollable">
              <table>
                <caption>
                  Requested and observed are separate columns. Units differ between the two
                  sides for memory, CPU and capabilities, and the agreement column compares them
                  per property rather than by string equality.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Property</th>
                    <th scope="col">Requested</th>
                    <th scope="col">Observed</th>
                    <th scope="col">Agreement</th>
                  </tr>
                </thead>
                <tbody>
                  {evidence.facts.map((fact) => (
                    <tr key={fact.name}>
                      <th scope="row" style={{ fontWeight: 400 }}>
                        {fact.name}
                      </th>
                      <td className="mono">{fact.requested ?? '—'}</td>
                      <td className="mono">{fact.observed ?? '—'}</td>
                      <td>
                        <span className={`badge badge--${agreementFamily(fact)}`}>
                          {agreementLabel(fact)}
                        </span>
                      </td>
                    </tr>
                  ))}
                  {evidence.facts.length === 0 && (
                    <tr>
                      <td colSpan={4}>No containment properties were recorded.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </ScrollRegion>

            {evidence.weaknesses.length > 0 && (
              <Panel quiet>
                <p className="limitations__title">Recorded weaknesses</p>
                <ul className="limitations">
                  {evidence.weaknesses.map((text) => (
                    <li key={text}>{text}</li>
                  ))}
                </ul>
              </Panel>
            )}

            <Panel quiet>
              <Limitations items={evidence.limitations} />
            </Panel>
          </section>
        );
      })}
    </>
  );
}
