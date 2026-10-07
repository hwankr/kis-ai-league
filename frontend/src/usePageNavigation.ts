import { useEffect, useSyncExternalStore } from 'react';
import type { ChartInterval, ChartRequest } from './types';

export const pages = [
  { id: 'account', label: '내 계좌' },
  { id: 'trades', label: '거래 내역' },
  { id: 'candidates', label: '후보 탐색' },
  { id: 'experiments', label: '실험실' },
  { id: 'chart', label: '시세·차트' },
] as const;
export type PageId = typeof pages[number]['id'];
const legacyPages: Record<string, PageId> = {
  '#main-content': 'account', '#account-summary': 'account', '#asset-history': 'account',
  '#trade-history': 'trades', '#candidate-comparison': 'candidates',
  '#stock-chart': 'chart', '#market-quotes': 'chart',
};

function subscribe(listener: () => void) {
  window.addEventListener('hashchange', listener);
  window.addEventListener('popstate', listener);
  return () => {
    window.removeEventListener('hashchange', listener);
    window.removeEventListener('popstate', listener);
  };
}

export function chartHref(request: ChartRequest) {
  return `#/chart?${new URLSearchParams({ ...request })}`;
}

export function navigate(href: string) {
  if (window.location.hash === href) return;
  window.history.pushState(null, '', href);
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}

export default function usePageNavigation() {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash);
  const [path, search = ''] = hash.replace(/^#\/?/, '').split('?');
  const page = legacyPages[hash] ?? pages.find(item => item.id === path)?.id ?? 'account';
  const params = new URLSearchParams(search);
  const rawSymbol = params.get('symbol');
  const symbol = rawSymbol?.trim().toUpperCase() ?? null;
  const interval = params.get('interval') ?? 'day';
  const invalidChart = page === 'chart' && ((rawSymbol !== null && !/^[0-9A-Z]{6}$/.test(symbol ?? ''))
    || !['day', '5m', '15m'].includes(interval));
  const request: ChartRequest | null = page === 'chart' && symbol && !invalidChart
    ? { symbol, interval: interval as ChartInterval } : null;

  useEffect(() => {
    if (legacyPages[hash]) {
      window.history.replaceState(null, '', `#/${legacyPages[hash]}`);
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    }
  }, [hash]);

  return { page, request, invalidChart, title: pages.find(item => item.id === page)!.label };
}
