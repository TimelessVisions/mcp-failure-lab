/**
 * Entry point. Loads the dataset, or renders why it could not.
 *
 * An error screen rather than a blank page: a viewer that failed silently would
 * leave every figure absent, and absent renders as zero to anyone reading
 * quickly. The failure names the paths that did not validate.
 */

import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from '@/app/App';
import { DatasetError, loadDataset } from '@/data/dataset';
import type { ViewerDataset } from '@/data/types';
import '@/app/theme.css';

function ErrorScreen({ error }: { error: DatasetError | Error }) {
  const problems = error instanceof DatasetError ? error.problems : [error.message];
  return (
    <div className="error-screen" role="alert">
      <p className="eyebrow">Evidence viewer</p>
      <h1 className="view-title">{error.message}</h1>
      <p>
        Nothing is rendered from a dataset that does not validate. A partial render would leave
        figures blank, and blank reads as zero.
      </p>
      <ul className="limitations">
        {problems.map((problem) => (
          <li key={problem} className="mono">
            {problem}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Root() {
  const [dataset, setDataset] = useState<ViewerDataset | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadDataset()
      .then((loaded) => {
        if (!cancelled) setDataset(loaded);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause : new Error(String(cause)));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error !== null) return <ErrorScreen error={error} />;
  if (dataset === null) {
    return (
      <div className="error-screen" style={{ borderColor: 'var(--rule-strong)' }}>
        <p className="eyebrow">Evidence viewer</p>
        <h1 className="view-title">Reading the dataset…</h1>
      </div>
    );
  }
  return <App dataset={dataset} />;
}

const container = document.getElementById('root');
if (container !== null) {
  createRoot(container).render(
    <StrictMode>
      <Root />
    </StrictMode>,
  );
}
