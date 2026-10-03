import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import HistoryChart from '../components/HistoryChart';
import type { HistoryData, Numeric } from '../types';
import { element } from './fixtures';

function data(values: Numeric[], error: string | null = null): HistoryData {
  return {
    points: values.map((total_value, index) => ({ observed_at: `2026-10-03T04:0${index}:00Z`, total_value, cash: null })),
    total_count: values.length,
    error,
  };
}

function chart(history: HistoryData | null) {
  return <HistoryChart history={history} accountId="practice" accountName="일반 모의계좌" />;
}

describe('asset history chart', () => {
  it('draws a single observation with an accessible account name and KST timestamp', () => {
    render(chart(data(['10000000'])));
    expect(screen.getByRole('img', { name: /일반 모의계좌 총자산 변화/ })).toBeTruthy();
    expect(element('asset-history').dataset.state).toBe('ready');
    expect(element('asset-history').dataset.accountId).toBe('practice');
    expect(element('history-chart').querySelectorAll('circle')).toHaveLength(1);
    expect(element('history-chart').innerHTML).not.toMatch(/NaN|Infinity/);
    expect(element('history-latest').textContent).toContain('10,000,000원');
    expect(element('history-latest').textContent).toContain('13:00:00 KST');
  });

  it('keeps a flat history finite and horizontal', () => {
    render(chart(data([100, 100, 100])));
    const dots = Array.from(element('history-chart').querySelectorAll('circle'));
    expect(dots).toHaveLength(3);
    expect(new Set(dots.map(dot => dot.getAttribute('cy'))).size).toBe(1);
    expect(new Set(dots.map(dot => dot.getAttribute('cx'))).size).toBe(3);
    expect(element('history-chart').querySelectorAll('polyline')).toHaveLength(1);
    expect(element('history-chart').innerHTML).not.toMatch(/NaN|Infinity/);
  });

  it('preserves missing total values as gaps, without drawing a line through them or treating them as zero', () => {
    render(chart({ ...data([100, null, 200]), total_count: 20 }));
    expect(element('asset-history').dataset.observationCount).toBe('3');
    expect(element('asset-history').dataset.pointCount).toBe('2');
    expect(element('history-count').textContent).toBe('최근 3 / 20회');
    expect(element('history-chart').querySelectorAll('polyline')).toHaveLength(0);
    expect(Array.from(element('history-chart').querySelectorAll('circle')).map(dot => dot.getAttribute('data-value'))).toEqual(['100', '200']);
  });

  it('distinguishes empty history from observations with missing total values', () => {
    const view = render(chart(data([])));
    expect(element('history-empty').textContent).toBe('저장된 조회 이력이 없습니다');
    expect(element('history-chart').hasAttribute('hidden')).toBe(true);
    view.rerender(chart(data([null, ''])));
    expect(element('history-empty').textContent).toBe('총자산 값이 있는 이력이 없습니다');
    expect(element('asset-history').dataset.observationCount).toBe('2');
    expect(element('asset-history').dataset.pointCount).toBe('0');
  });

  it('shows a storage error while retaining any readable history, then clears it for another account', () => {
    const view = render(chart(data([100, 200], '이력 저장 실패')));
    expect(element('asset-history').dataset.state).toBe('error');
    expect(element('history-error').hidden).toBe(false);
    expect(element('history-error').textContent).toBe('이력 저장 실패');
    expect(element('history-chart').hasAttribute('hidden')).toBe(false);
    view.rerender(<HistoryChart history={null} accountId="league" accountName="대회 계좌" placeholder={{ state: 'loading', message: '이력을 불러오는 중' }} />);
    expect(element('asset-history').dataset.accountId).toBe('league');
    expect(element('asset-history').dataset.pointCount).toBe('0');
    expect(element('history-chart').querySelectorAll('circle')).toHaveLength(0);
    expect(element('history-error').hidden).toBe(true);
    expect(element('history-empty').textContent).toBe('이력을 불러오는 중');
  });
});
