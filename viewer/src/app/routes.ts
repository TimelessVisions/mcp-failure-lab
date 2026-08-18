/**
 * Hash routing, hand-rolled and deliberately small.
 *
 * A hash route needs no server rewrite rule, which matters because this is
 * served by whatever static file server is to hand — often `python -m
 * http.server` on a machine with the network off. A history-API router would
 * 404 on every deep link in exactly that setting.
 */

import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react';

export type Route =
  | { name: 'overview' }
  | { name: 'matrix' }
  | { name: 'categories'; category?: string }
  | { name: 'target'; targetKey: string }
  | { name: 'scenario'; scenarioId: string }
  | { name: 'signature'; targetKey?: string; scenarioId?: string }
  | { name: 'capabilities' }
  | { name: 'containment' }
  | { name: 'timings' }
  | { name: 'reproduction' }
  | { name: 'methodology' };

export const OVERVIEW: Route = { name: 'overview' };

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#\/?/, '');
  const [pathPart, queryPart] = raw.split('?');
  const segments = (pathPart ?? '').split('/').filter(Boolean).map(decodeURIComponent);
  const query = new URLSearchParams(queryPart ?? '');
  const [head, tail] = segments;

  switch (head) {
    case undefined:
    case '':
      return OVERVIEW;
    case 'matrix':
      return { name: 'matrix' };
    case 'categories':
      return tail === undefined
        ? { name: 'categories' }
        : { name: 'categories', category: tail };
    case 'targets':
      return tail === undefined ? { name: 'matrix' } : { name: 'target', targetKey: tail };
    case 'scenarios':
      return tail === undefined ? { name: 'matrix' } : { name: 'scenario', scenarioId: tail };
    case 'signatures': {
      const targetKey = query.get('target');
      const scenarioId = query.get('scenario');
      return {
        name: 'signature',
        ...(targetKey === null ? {} : { targetKey }),
        ...(scenarioId === null ? {} : { scenarioId }),
      };
    }
    case 'capabilities':
      return { name: 'capabilities' };
    case 'containment':
      return { name: 'containment' };
    case 'timings':
      return { name: 'timings' };
    case 'reproduction':
      return { name: 'reproduction' };
    case 'methodology':
      return { name: 'methodology' };
    default:
      return OVERVIEW;
  }
}

export function hrefFor(route: Route): string {
  switch (route.name) {
    case 'overview':
      return '#/';
    case 'matrix':
      return '#/matrix';
    case 'categories':
      return route.category === undefined
        ? '#/categories'
        : `#/categories/${encodeURIComponent(route.category)}`;
    case 'target':
      return `#/targets/${encodeURIComponent(route.targetKey)}`;
    case 'scenario':
      return `#/scenarios/${encodeURIComponent(route.scenarioId)}`;
    case 'signature': {
      const query = new URLSearchParams();
      if (route.targetKey !== undefined) query.set('target', route.targetKey);
      if (route.scenarioId !== undefined) query.set('scenario', route.scenarioId);
      const suffix = query.toString();
      return suffix === '' ? '#/signatures' : `#/signatures?${suffix}`;
    }
    case 'capabilities':
      return '#/capabilities';
    case 'containment':
      return '#/containment';
    case 'timings':
      return '#/timings';
    case 'reproduction':
      return '#/reproduction';
    case 'methodology':
      return '#/methodology';
  }
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange);
  return () => window.removeEventListener('hashchange', onChange);
}

function snapshot(): string {
  return window.location.hash;
}

export function useRoute(): { route: Route; navigate: (next: Route) => void } {
  const hash = useSyncExternalStore(subscribe, snapshot, () => '#/');
  const navigate = useCallback((next: Route) => {
    window.location.hash = hrefFor(next);
  }, []);
  return { route: parseHash(hash), navigate };
}

/**
 * Move focus to the view heading whenever the route changes.
 *
 * Without this a keyboard user who follows a link stays parked at the end of the
 * navigation list, and a screen reader announces nothing at all — the page
 * changed underneath them with no signal.
 *
 * Deliberately not on first mount. Focusing the heading as the page loads puts
 * the caret past the skip link, which makes the skip link unreachable by the one
 * keystroke it exists for.
 */
export function useRouteFocus(route: Route, ref: React.RefObject<HTMLElement | null>): void {
  const key = hrefFor(route);
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    ref.current?.focus();
  }, [key, ref]);
}
