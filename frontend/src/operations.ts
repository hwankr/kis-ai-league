export const mirrorStates = {
  stopped: '정지', starting: '시작 중', initialized: '연결 확인 중', paused: '신규 주문 정지',
  waiting_market: '장 시작 대기', idle: '체결 대기', working: '연동 중', blocked: '확인 필요',
  source_changed: '원천 계좌 변경', error: '오류', stopping: '정지 중', unverified: '실행 확인 필요',
} as const;

export type OperationCommand = { action: 'mirror_start' | 'mirror_stop' };
export interface OperationsData {
  dashboard: { status: 'ok' | 'stalled'; updated_at: string | null };
  mirror: { status: keyof typeof mirrorStates; updated_at: string | null; reason: string | null;
    can_start: boolean; can_stop: boolean };
  busy: boolean;
  error: string | null;
}

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const nullableString = (value: unknown): value is string | null => value === null || typeof value === 'string';

export function readOperations(value: unknown): OperationsData {
  if (!object(value) || !object(value.dashboard) || !object(value.mirror)
    || !['ok', 'stalled'].includes(String(value.dashboard.status))
    || !nullableString(value.dashboard.updated_at)
    || typeof value.mirror.status !== 'string' || !Object.hasOwn(mirrorStates, value.mirror.status)
    || !nullableString(value.mirror.updated_at) || !nullableString(value.mirror.reason)
    || typeof value.mirror.can_start !== 'boolean' || typeof value.mirror.can_stop !== 'boolean'
    || typeof value.busy !== 'boolean' || !nullableString(value.error)) {
    throw new Error('실행 상태 응답을 확인하지 못했습니다.');
  }
  return value as unknown as OperationsData;
}
