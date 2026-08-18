/**
 * One category at a time, with its own denominator and its own limitations.
 *
 * Categories exist because "how did this implementation do on stateful
 * lifecycle" and "how did it do on process containment" are different questions
 * with different evidence behind them. Averaging them produces a number that
 * answers neither, so this screen never sums across categories and never orders
 * implementations within one.
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
import { categoryNames, describeDenominator, summariesForCategory } from '@/data/selectors';
import type { ViewerDataset } from '@/data/types';

export function Categories({
  dataset,
  category,
  headingRef,
}: {
  dataset: ViewerDataset;
  category?: string | undefined;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const names = categoryNames(dataset);
  const active = category !== undefined && names.includes(category) ? category : undefined;
  const shown = active === undefined ? names : [active];

  return (
    <>
      <ViewHeader
        eyebrow="Categories"
        title={active ?? 'Every benchmark category'}
        lede={
          <p>
            Each category carries its own applicable denominator and its own limitation. Nothing
            here sums across categories, because the counts are not commensurable: they rest on
            different evidence and answer different questions.
          </p>
        }
        headingRef={headingRef}
      />

      <nav className="toolbar" aria-label="Filter by category">
        <a
          className="tag"
          href={hrefFor({ name: 'categories' })}
          aria-current={active === undefined ? 'page' : undefined}
        >
          all
        </a>
        {names.map((name) => (
          <a
            key={name}
            className="tag"
            href={hrefFor({ name: 'categories', category: name })}
            aria-current={active === name ? 'page' : undefined}
          >
            {name}
          </a>
        ))}
      </nav>

      {shown.map((name) => {
        const summaries = summariesForCategory(dataset, name);
        const limitations = [...new Set(summaries.flatMap((entry) => entry.limitations))];
        const securityDimension = summaries.find((entry) => entry.security_dimension !== '');

        return (
          <section className="section" key={name} aria-labelledby={`category-${name}`}>
            <h2 className="section__title" id={`category-${name}`}>
              {name}
            </h2>
            <ScrollRegion label="Category counts, scrollable">
              <table>
                <caption>
                  Counts are over scenarios, not samples: a scenario measured three times
                  contributes one result, and only when all three repetitions agreed on a
                  complete signature.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Implementation</th>
                    <th scope="col" className="numeric">
                      Applicable
                    </th>
                    <th scope="col" className="numeric">
                      Executed
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
                    <th scope="col" className="numeric">
                      Not measured
                    </th>
                    <th scope="col" className="numeric">
                      Security-relevant
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {summaries
                    .slice()
                    .sort((a, b) => (a.target_key ?? '').localeCompare(b.target_key ?? ''))
                    .map((summary) => {
                      const target = dataset.targets.find(
                        (entry) => entry.target_key === summary.target_key,
                      );
                      return (
                        <tr key={`${name}-${summary.target_key ?? 'all'}`}>
                          <th scope="row" style={{ fontWeight: 400 }}>
                            {summary.target_key === null ? (
                              'all implementations'
                            ) : (
                              <a
                                href={hrefFor({
                                  name: 'target',
                                  targetKey: summary.target_key,
                                })}
                              >
                                {target?.display_name ?? summary.target_key}
                              </a>
                            )}
                          </th>
                          <td className="numeric">{summary.applicable_scenarios}</td>
                          <td className="numeric">{summary.executed_scenarios}</td>
                          <td className="numeric">
                            {describeDenominator(
                              summary.stable_pass,
                              summary.applicable_scenarios,
                            )}
                          </td>
                          <td className="numeric">{summary.stable_non_pass}</td>
                          <td className="numeric">{summary.inconclusive}</td>
                          <td className="numeric">{summary.not_applicable}</td>
                          <td className="numeric">{summary.not_measured}</td>
                          <td className="numeric">{summary.security_relevant_observations}</td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </ScrollRegion>

            <Panel quiet>
              <FieldList>
                <Field
                  label="Security dimension"
                  value={securityDimension?.security_dimension ?? null}
                  prose
                  absentNote="this category records no security-specific reading"
                />
              </FieldList>
              <Limitations items={limitations} title="Category limitations" />
            </Panel>
          </section>
        );
      })}
    </>
  );
}
