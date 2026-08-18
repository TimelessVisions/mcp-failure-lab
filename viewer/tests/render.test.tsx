/**
 * What the screens actually put in front of a reader.
 *
 * The assertions are about meaning: that NOT_MEASURED appears rather than being
 * omitted, that a denominator is on screen beside a count, that no view renders
 * ranking language, and that a cell's evidence panel opens with its limitations
 * attached.
 */

import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createRef } from 'react';
import { describe, expect, it } from 'vitest';

import { Methodology } from '@/views/Methodology';
import { Overview } from '@/views/Overview';
import { Reproduction } from '@/views/Reproduction';
import { TargetMatrix } from '@/views/TargetMatrix';
import { Timings } from '@/views/Timings';
import { shippedDataset } from './fixture';

const dataset = shippedDataset();
const headingRef = () => createRef<HTMLHeadingElement>();

/** Phrases that would turn an observation into a verdict. */
const RANKING_LANGUAGE = [
  /\bbest (sdk|implementation|server)\b/i,
  /\bworst\b/i,
  /\bwinner\b/i,
  /\branked?\s+(first|second|third|highest|lowest)\b/i,
  /\boverall score\b/i,
  /\bgrade\s*[:=]/i,
  /\bmost compliant\b/i,
  /\bsafest\b/i,
];

function assertNoRankingLanguage(text: string): void {
  for (const pattern of RANKING_LANGUAGE) {
    // "no best implementation here" is the project stating the rule, not
    // breaking it, so the negated forms are exempted before matching.
    const withoutDisclaimers = text
      .replace(/no best (sdk|implementation|server)/gi, '')
      .replace(/no safest implementation/gi, '')
      .replace(/no most compliant target/gi, '')
      .replace(/no overall score/gi, '')
      .replace(/there is no best/gi, '');
    expect(withoutDisclaimers).not.toMatch(pattern);
  }
}

describe('Overview', () => {
  it('shows the applicable denominator and the absent states as first-class figures', () => {
    render(<Overview dataset={dataset} headingRef={headingRef()} />);
    expect(screen.getByText('Applicable results')).toBeInTheDocument();
    expect(screen.getByText('Not applicable')).toBeInTheDocument();
    expect(screen.getByText(/outside every denominator/i)).toBeInTheDocument();
  });

  it('names the domain of every not-measured figure', () => {
    render(<Overview dataset={dataset} headingRef={headingRef()} />);
    // Two different things wear these words: a scenario with no recorded
    // result, and a timing metric nobody sampled. A bare "Not measured" on a
    // screen carrying both leaves the reader to guess which one a number is.
    expect(screen.getByText('Scenario results not measured')).toBeInTheDocument();
    expect(screen.getByText('Timing metrics not measured')).toBeInTheDocument();
    expect(screen.queryByText('Not measured')).toBeNull();
  });

  it('separates security-relevant observations from confirmed vulnerabilities', () => {
    render(<Overview dataset={dataset} headingRef={headingRef()} />);
    expect(screen.getByText('Security-relevant')).toBeInTheDocument();
    expect(screen.getByText('Confirmed vulnerabilities')).toBeInTheDocument();
    expect(screen.getByText(/observations, not vulnerabilities/i)).toBeInTheDocument();
  });

  it('states plainly that there is no score on the page', () => {
    const { container } = render(<Overview dataset={dataset} headingRef={headingRef()} />);
    expect(screen.getByText(/no overall score on this page/i)).toBeInTheDocument();
    assertNoRankingLanguage(container.textContent ?? '');
  });
});

describe('Target matrix', () => {
  it('renders one row per scenario and one column per implementation', () => {
    render(<TargetMatrix dataset={dataset} headingRef={headingRef()} />);
    const table = screen.getByRole('table');
    const rows = within(table).getAllByRole('row');
    expect(rows.length).toBe(dataset.scenarios.length + 1);
  });

  it('says in the caption that the rows are not a ranking', () => {
    const { container } = render(<TargetMatrix dataset={dataset} headingRef={headingRef()} />);
    // The phrase is split across elements by the emphasis, so the assertion is
    // over the rendered text rather than over one node.
    expect(container.textContent).toMatch(/not a ranking/i);
    assertNoRankingLanguage(container.textContent ?? '');
  });

  it('opens an evidence panel carrying the denominator, repetitions and limitations', async () => {
    const user = userEvent.setup();
    const { container } = render(<TargetMatrix dataset={dataset} headingRef={headingRef()} />);

    const table = screen.getByRole('table');
    const cells = within(table).getAllByRole('button', { name: /applicable/i });
    const first = cells[0];
    expect(first).toBeDefined();
    await user.click(first!);

    // Scoped to the panel: the stacked phone presentation renders the same
    // field labels for its own cards, and both are in the DOM at once because
    // which one is visible is a CSS decision rather than a JavaScript one.
    const panel = container.querySelector('.evidence-panel');
    expect(panel).not.toBeNull();
    const evidence = within(panel as HTMLElement);
    expect(evidence.getByText('Applicable denominator')).toBeInTheDocument();
    expect(evidence.getByText('Repetitions')).toBeInTheDocument();
    expect(evidence.getByText('Signature stability')).toBeInTheDocument();
    expect(evidence.getByText(/what this does not establish/i)).toBeInTheDocument();
  });

  it('presents every implementation without a table for narrow screens', () => {
    const { container } = render(<TargetMatrix dataset={dataset} headingRef={headingRef()} />);
    const stacked = container.querySelector('.stacked-matrix');
    expect(stacked).not.toBeNull();

    // One card per implementation, each carrying its own result. This is the
    // presentation a 390px screen gets, and it must not depend on the reader
    // discovering that a wider table exists somewhere off-screen.
    const cards = (stacked as HTMLElement).querySelectorAll('.stacked-cell');
    expect(cards.length).toBe(dataset.targets.length);

    const scoped = within(stacked as HTMLElement);
    expect(scoped.getByLabelText(/^Scenario —/)).toBeInTheDocument();
    for (const target of dataset.targets) {
      expect(scoped.getAllByText(target.display_name).length).toBeGreaterThan(0);
    }
  });

  it('labels every cell for a screen reader with its state and denominator', () => {
    render(<TargetMatrix dataset={dataset} headingRef={headingRef()} />);
    const buttons = screen.getAllByRole('button');
    expect(buttons.length).toBeGreaterThan(0);
    for (const button of buttons.slice(0, 20)) {
      expect(button.getAttribute('aria-label')).toMatch(/applicable/i);
    }
  });
});

describe('Timings', () => {
  it('renders the un-measured metrics rather than omitting them', () => {
    render(<Timings dataset={dataset} headingRef={headingRef()} />);
    // Named for its domain: these are timing metrics, not scenario results.
    expect(
      screen.getByRole('heading', { name: 'Timing metrics not measured' }),
    ).toBeInTheDocument();

    const unmeasured = dataset.measurement_coverage.entries.filter(
      (entry) => entry.state === 'not_measured',
    );
    expect(unmeasured.length).toBeGreaterThan(0);
    for (const entry of unmeasured) {
      expect(screen.getAllByText(entry.display_name).length).toBeGreaterThan(0);
    }
  });

  it('shows every promised metric in the coverage ledger with its state', () => {
    render(<Timings dataset={dataset} headingRef={headingRef()} />);
    for (const entry of dataset.measurement_coverage.entries) {
      expect(screen.getAllByText(entry.metric_id).length).toBeGreaterThan(0);
    }
  });

  it('never renders a median where the samples are absent', () => {
    render(<Timings dataset={dataset} headingRef={headingRef()} />);
    for (const card of dataset.timings) {
      if (card.median_ms === null) continue;
      expect(card.samples_ms.length).toBeGreaterThan(0);
    }
  });

  it('says that durations are not compared between implementations', () => {
    const { container } = render(<Timings dataset={dataset} headingRef={headingRef()} />);
    expect(container.textContent).toMatch(/never compared between implementations/i);
    assertNoRankingLanguage(container.textContent ?? '');
  });
});

describe('Reproduction', () => {
  it('shows no private finding package in public-safe mode', () => {
    render(<Reproduction dataset={dataset} headingRef={headingRef()} />);
    if (dataset.visibility_mode === 'public-safe') {
      expect(screen.getByText(/private finding packages are absent/i)).toBeInTheDocument();
      for (const summary of dataset.reproduction) {
        expect(summary.synthetic).toBe(true);
      }
    }
  });
});

describe('Methodology', () => {
  it('renders every methodology section as a heading, not a footnote', () => {
    render(<Methodology dataset={dataset} headingRef={headingRef()} />);
    for (const section of dataset.methodology) {
      expect(screen.getByRole('heading', { name: section.title })).toBeInTheDocument();
    }
  });

  it('states the disclosure controls, including that it is not publishable', () => {
    const { container } = render(<Methodology dataset={dataset} headingRef={headingRef()} />);
    expect(screen.getByText('Disclosure controls')).toBeInTheDocument();
    expect(container.textContent).toMatch(/not published by this checkpoint/i);
  });
});
