/**
 * The shell: navigation, the mode badge, and the one place a route becomes a view.
 *
 * The mode badge is not decoration. A private dataset and a public-safe one look
 * similar at a glance and are governed by completely different rules, so the
 * mode is stated in the chrome of every screen rather than on a settings page
 * somebody has to go and find.
 */

import { useEffect, useRef, useState } from 'react';

import { EvidenceDisclaimer } from '@/components/primitives';
import { GuidedMode } from '@/guided/GuidedMode';
import { hrefFor, useRoute, useRouteFocus, type Route } from '@/app/routes';
import { Capabilities } from '@/views/Capabilities';
import { Categories } from '@/views/Categories';
import { Containment } from '@/views/Containment';
import { Methodology } from '@/views/Methodology';
import { Overview } from '@/views/Overview';
import { Reproduction } from '@/views/Reproduction';
import { ResultSignatureView } from '@/views/ResultSignature';
import { ScenarioDetail } from '@/views/ScenarioDetail';
import { TargetDetail } from '@/views/TargetDetail';
import { TargetMatrix } from '@/views/TargetMatrix';
import { Timings } from '@/views/Timings';
import type { ViewerDataset } from '@/data/types';

interface NavEntry {
  route: Route;
  label: string;
  group: string;
}

const NAV: NavEntry[] = [
  { route: { name: 'overview' }, label: 'Overview', group: 'Evidence' },
  { route: { name: 'matrix' }, label: 'Target matrix', group: 'Evidence' },
  { route: { name: 'categories' }, label: 'Categories', group: 'Evidence' },
  { route: { name: 'signature' }, label: 'Result signature', group: 'Evidence' },
  { route: { name: 'capabilities' }, label: 'Capability surface', group: 'Execution' },
  { route: { name: 'containment' }, label: 'Containment', group: 'Execution' },
  { route: { name: 'timings' }, label: 'Timings', group: 'Execution' },
  { route: { name: 'reproduction' }, label: 'Reproduction', group: 'Execution' },
  { route: { name: 'methodology' }, label: 'Methodology', group: 'Reading the evidence' },
];

function isCurrent(route: Route, entry: NavEntry): boolean {
  if (route.name === entry.route.name) return true;
  if (entry.route.name === 'matrix' && (route.name === 'target' || route.name === 'scenario')) {
    return true;
  }
  return false;
}

export function App({ dataset }: { dataset: ViewerDataset }) {
  const { route } = useRoute();
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const [guided, setGuided] = useState(false);
  useRouteFocus(route, headingRef);

  useEffect(() => {
    document.title = `${titleFor(route)} — ${dataset.project.name}`;
  }, [route, dataset.project.name]);

  const groups = [...new Set(NAV.map((entry) => entry.group))];

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to the evidence
      </a>

      <div className="shell">
        <nav className="shell__nav" aria-label="Views">
          <div className="brand">
            <div className="brand__mark">Evidence viewer · static · offline</div>
            <div className="brand__name">{dataset.project.name}</div>
            <div style={{ marginTop: 'var(--space-3)' }}>
              <span className="badge badge--mode">{dataset.visibility_mode}</span>
            </div>
          </div>

          {groups.map((group) => (
            <div key={group}>
              <p className="nav-group">{group}</p>
              <div className="nav-list">
                {NAV.filter((entry) => entry.group === group).map((entry, position) => (
                  <a
                    key={entry.label}
                    className="nav-link"
                    href={hrefFor(entry.route)}
                    aria-current={isCurrent(route, entry) ? 'page' : undefined}
                  >
                    <span className="nav-link__index">
                      {String(NAV.indexOf(entry) + 1 - position * 0).padStart(2, '0')}
                    </span>
                    {entry.label}
                  </a>
                ))}
              </div>
            </div>
          ))}

          {/*
           * Both modes get a walkthrough, and they are different walkthroughs.
           * The private one presents real findings; the public-safe one
           * presents the method over synthetic data. The button is named for
           * whichever this build actually has, so the two can never be
           * mistaken for one another in a recording.
           */}
          <div style={{ padding: 'var(--space-4) var(--space-5) 0' }}>
            <button
              type="button"
              className="button button--primary"
              onClick={() => setGuided(true)}
            >
              {dataset.visibility_mode === 'private'
                ? 'Guided walkthrough'
                : 'Guided walkthrough (synthetic)'}
            </button>
            <p
              style={{
                fontSize: '0.6875rem',
                color: 'var(--text-quiet)',
                marginTop: 'var(--space-2)',
              }}
            >
              Arrow keys to move, Esc to exit.
            </p>
          </div>
        </nav>

        <main className="shell__main" id="main">
          {/*
           * Above the view, on every route. A disclaimer that appeared only on
           * the overview would be absent from exactly the screens someone links
           * to when they want to show a result.
           */}
          <EvidenceDisclaimer provenance={dataset.provenance} />
          <View route={route} dataset={dataset} headingRef={headingRef} />
        </main>
      </div>

      {guided && <GuidedMode dataset={dataset} onExit={() => setGuided(false)} />}
    </>
  );
}

function titleFor(route: Route): string {
  const entry = NAV.find((candidate) => candidate.route.name === route.name);
  if (entry !== undefined) return entry.label;
  if (route.name === 'target') return 'Implementation';
  if (route.name === 'scenario') return 'Scenario';
  return 'Evidence';
}

function View({
  route,
  dataset,
  headingRef,
}: {
  route: Route;
  dataset: ViewerDataset;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  switch (route.name) {
    case 'overview':
      return <Overview dataset={dataset} headingRef={headingRef} />;
    case 'matrix':
      return <TargetMatrix dataset={dataset} headingRef={headingRef} />;
    case 'categories':
      return <Categories dataset={dataset} category={route.category} headingRef={headingRef} />;
    case 'target':
      return (
        <TargetDetail dataset={dataset} targetKey={route.targetKey} headingRef={headingRef} />
      );
    case 'scenario':
      return (
        <ScenarioDetail
          dataset={dataset}
          scenarioId={route.scenarioId}
          headingRef={headingRef}
        />
      );
    case 'signature':
      return (
        <ResultSignatureView
          dataset={dataset}
          targetKey={route.targetKey}
          scenarioId={route.scenarioId}
          headingRef={headingRef}
        />
      );
    case 'capabilities':
      return <Capabilities dataset={dataset} headingRef={headingRef} />;
    case 'containment':
      return <Containment dataset={dataset} headingRef={headingRef} />;
    case 'timings':
      return <Timings dataset={dataset} headingRef={headingRef} />;
    case 'reproduction':
      return <Reproduction dataset={dataset} headingRef={headingRef} />;
    case 'methodology':
      return <Methodology dataset={dataset} headingRef={headingRef} />;
  }
}
