/**
 * What each server advertised, and how far that was observed rather than read.
 *
 * Declared and observed sit in adjacent columns and are never merged. A target
 * file saying "this server exposes an echo tool" is a person's claim; the tool
 * list the server answered with is the server's behaviour. Substituting one for
 * the other is precisely the error the applicability rules exist to prevent, and
 * it would be invisible on a screen that showed only "tools".
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
import type { ViewerDataset } from '@/data/types';

export function Capabilities({
  dataset,
  headingRef,
}: {
  dataset: ViewerDataset;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  return (
    <>
      <ViewHeader
        eyebrow="Capability surface"
        title="What each implementation advertised"
        lede={
          <p>
            The surface is observed once per implementation and every later sample is held to
            it. A surface that moved mid-execution rejects the samples on both sides of the move
            rather than being averaged, because the applicability decisions on either side
            rested on different evidence.
          </p>
        }
        headingRef={headingRef}
      />

      {dataset.capabilities.length === 0 && (
        <p className="callout callout--absent">
          This execution recorded no capability surfaces.
        </p>
      )}

      {dataset.capabilities.map((surface) => {
        const target = dataset.targets.find((entry) => entry.target_key === surface.target_key);
        return (
          <section className="section" key={surface.target_key}>
            <h2 className="section__title">
              <a href={hrefFor({ name: 'target', targetKey: surface.target_key })}>
                {target?.display_name ?? surface.target_key}
              </a>
            </h2>

            <div className="grid grid--2">
              <Panel quiet title="Negotiation">
                <FieldList>
                  <Field label="Resolution status" value={surface.status} />
                  <Field
                    label="Detail"
                    value={surface.status_detail}
                    prose
                    absentNote="no further detail recorded"
                  />
                  <Field label="Profile offered" value={surface.offered_profile} />
                  <Field label="Profile negotiated" value={surface.negotiated_profile} />
                  <Field
                    label="Capability fingerprint"
                    value={surface.capability_fingerprint}
                    absentNote="withheld in public-safe mode"
                  />
                  <Field
                    label="Fingerprint stable across repetitions"
                    value={
                      surface.fingerprint_stable_across_repetitions === null
                        ? null
                        : surface.fingerprint_stable_across_repetitions
                          ? 'yes — one surface throughout'
                          : 'no — the surface moved during the execution'
                    }
                    absentNote="no repetition carried a fingerprint to compare"
                  />
                  <Field
                    label="Server capabilities"
                    value={Object.entries(surface.server_capabilities)
                      .map(([key, value]) => `${key}: ${value}`)
                      .join(' · ')}
                    absentNote="the server advertised none"
                  />
                  <Field
                    label="Resources"
                    value={surface.advertises_resources ? 'yes' : 'no'}
                  />
                  <Field label="Prompts" value={surface.advertises_prompts ? 'yes' : 'no'} />
                  <Field label="Logging" value={surface.advertises_logging ? 'yes' : 'no'} />
                  <Field
                    label="Completions"
                    value={surface.advertises_completions ? 'yes' : 'no'}
                  />
                </FieldList>
              </Panel>

              <Panel quiet title="Observed transport features">
                <FieldList>
                  {Object.entries(surface.observed_transport_features).map(([key, value]) => (
                    <Field key={key} label={key} value={value} />
                  ))}
                </FieldList>
                {Object.keys(surface.observed_transport_features).length === 0 && (
                  <p className="callout callout--absent">
                    Nothing was observed about this transport in this execution.
                  </p>
                )}
              </Panel>
            </div>

            <ScrollRegion label="Observed tools, scrollable">
              <table>
                <caption>
                  Observed tools, with the schema fingerprints that decide whether two runs
                  measured the same surface.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Tool</th>
                    <th scope="col">Input schema fingerprint</th>
                    <th scope="col">Output schema fingerprint</th>
                  </tr>
                </thead>
                <tbody>
                  {surface.tools.map((tool) => (
                    <tr key={tool.name}>
                      <th scope="row" className="mono" style={{ fontWeight: 400 }}>
                        {tool.name}
                      </th>
                      <td className="mono">{tool.input_schema_fingerprint ?? '—'}</td>
                      <td className="mono">{tool.output_schema_fingerprint ?? '—'}</td>
                    </tr>
                  ))}
                  {surface.tools.length === 0 && (
                    <tr>
                      <td colSpan={3}>The server advertised no tools.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </ScrollRegion>

            <Panel quiet>
              <FieldList>
                <Field
                  label="Declared in the target file"
                  value={surface.declared_tools.join(', ')}
                  absentNote="the target file declares none"
                  prose
                />
                <Field
                  label="Declared against observed"
                  value={surface.declared_versus_observed.join(' · ')}
                  absentNote="the declaration and the observation agree"
                  prose
                />
              </FieldList>
              <Limitations items={surface.limitations} />
            </Panel>
          </section>
        );
      })}
    </>
  );
}
