/**
 * The rules, as a screen rather than a footer.
 *
 * Every rule here is one a reader would otherwise infer from the shape of a
 * table, and inferring them wrongly is how a category-level observation becomes
 * "this SDK is worse". A footer is where a rule goes to be skipped; this is a
 * destination in the navigation with the same standing as the results.
 */

import { Field, FieldList, Panel, ViewHeader } from '@/components/primitives';
import type { ViewerDataset } from '@/data/types';

export function Methodology({
  dataset,
  headingRef,
}: {
  dataset: ViewerDataset;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const scopes = [...new Set(dataset.limitations.map((entry) => entry.scope))];
  const controls = dataset.disclosure_controls;

  return (
    <>
      <ViewHeader
        eyebrow="Methodology and limitations"
        title="What these figures mean, and what they do not"
        lede={
          <p>
            Nothing on this screen is boilerplate. Each rule decides how a number elsewhere in
            this viewer should be read, and several of them exist because the obvious reading is
            wrong.
          </p>
        }
        headingRef={headingRef}
      />

      {dataset.methodology.map((section) => (
        <section className="section" key={section.section_id} id={section.section_id}>
          <h2 className="section__title">{section.title}</h2>
          {section.body.map((paragraph) => (
            <p key={paragraph}>{paragraph}</p>
          ))}
        </section>
      ))}

      <section className="section" aria-labelledby="limitations">
        <h2 className="section__title" id="limitations">
          Limitations
        </h2>
        {scopes.map((scope) => (
          <Panel key={scope} quiet title={scope}>
            <ul className="limitations">
              {dataset.limitations
                .filter((entry) => entry.scope === scope)
                .map((entry) => (
                  <li key={entry.text}>{entry.text}</li>
                ))}
            </ul>
          </Panel>
        ))}
      </section>

      <section className="section" aria-labelledby="disclosure">
        <h2 className="section__title" id="disclosure">
          Disclosure controls
        </h2>
        <Panel quiet>
          <FieldList>
            <Field label="Visibility mode" value={controls.visibility_mode} />
            <Field
              label="Publishable"
              value={
                controls.publishable
                  ? 'yes'
                  : 'no — not published by this checkpoint, in whole or in part'
              }
              prose
            />
            <Field
              label="Anonymisation approved by the owner"
              value={controls.anonymization_approved ? 'yes' : 'no'}
            />
            <Field
              label="Approval reference"
              value={controls.approval_reference}
              prose
              absentNote="none recorded"
            />
            <Field
              label="Denylist terms scanned for"
              value={controls.denylist_term_count === 0 ? null : controls.denylist_term_count}
              absentNote="no scan is recorded on this dataset"
            />
            <Field
              label="Redactions applied"
              value={
                <ul className="limitations">
                  {controls.redactions_applied.map((text) => (
                    <li key={text}>{text}</li>
                  ))}
                </ul>
              }
              absentNote="none — this is the full-evidence dataset"
            />
          </FieldList>
          <ul className="limitations">
            {controls.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </Panel>
      </section>
    </>
  );
}
