/**
 * Routing must be total: every hash resolves to a view, including nonsense.
 *
 * A deep link that resolved to nothing would leave the shell rendered around an
 * empty main region, which reads as "there is no evidence here" rather than as
 * "that URL is wrong".
 */

import { describe, expect, it } from 'vitest';

import { hrefFor, parseHash, type Route } from '@/app/routes';

describe('parseHash', () => {
  it('treats an empty hash as the overview', () => {
    expect(parseHash('')).toEqual({ name: 'overview' });
    expect(parseHash('#')).toEqual({ name: 'overview' });
    expect(parseHash('#/')).toEqual({ name: 'overview' });
  });

  it('resolves an unknown route to the overview rather than to nothing', () => {
    expect(parseHash('#/there-is-no-such-view')).toEqual({ name: 'overview' });
  });

  it('reads a target and a scenario out of the path', () => {
    expect(parseHash('#/targets/implementation-a')).toEqual({
      name: 'target',
      targetKey: 'implementation-a',
    });
    expect(parseHash('#/scenarios/wire.malformed_json')).toEqual({
      name: 'scenario',
      scenarioId: 'wire.malformed_json',
    });
  });

  it('reads a signature selection out of the query', () => {
    expect(
      parseHash('#/signatures?target=implementation-a&scenario=wire.malformed_json'),
    ).toEqual({
      name: 'signature',
      targetKey: 'implementation-a',
      scenarioId: 'wire.malformed_json',
    });
  });

  it('falls back to the matrix when a detail route names nothing', () => {
    expect(parseHash('#/targets')).toEqual({ name: 'matrix' });
    expect(parseHash('#/scenarios')).toEqual({ name: 'matrix' });
  });

  it('round-trips every route through its href', () => {
    const routes: Route[] = [
      { name: 'overview' },
      { name: 'matrix' },
      { name: 'categories' },
      { name: 'categories', category: 'timing_and_recovery' },
      { name: 'target', targetKey: 'implementation-b' },
      { name: 'scenario', scenarioId: 'timing.http_slow_body' },
      { name: 'signature', targetKey: 'implementation-b', scenarioId: 'timing.http_slow_body' },
      { name: 'capabilities' },
      { name: 'containment' },
      { name: 'timings' },
      { name: 'reproduction' },
      { name: 'methodology' },
    ];
    for (const route of routes) {
      expect(parseHash(hrefFor(route))).toEqual(route);
    }
  });
});
