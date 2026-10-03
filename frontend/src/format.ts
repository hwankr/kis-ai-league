const numberFormatter = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 2 });
const quantityFormatter = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 6 });
const percentFormatter = new Intl.NumberFormat('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const timeFormatter = new Intl.DateTimeFormat('ko-KR', {
  timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
});
export function numeric(value: unknown): number | null {
  if ((typeof value !== 'number' && typeof value !== 'string') || String(value).trim() === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
export function number(value: unknown, signed = false): string {
  const parsed = numeric(value);
  return parsed === null ? '—' : `${signed && parsed > 0 ? '+' : ''}${numberFormatter.format(parsed)}`;
}
export function quantity(value: unknown): string {
  const parsed = numeric(value);
  return parsed === null ? '—' : `${quantityFormatter.format(parsed)}주`;
}
export function percent(value: unknown): string {
  const parsed = numeric(value);
  return parsed === null ? '—' : `${parsed > 0 ? '+' : ''}${percentFormatter.format(parsed)}%`;
}
export function signClass(value: unknown): string {
  const parsed = numeric(value);
  return parsed !== null && parsed > 0 ? 'gain' : parsed !== null && parsed < 0 ? 'loss' : '';
}
export function timestamp(value: string | null): { text: string; dateTime?: string } {
  const date = value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime())
    ? { text: `${timeFormatter.format(date)} KST`, dateTime: date.toISOString() } : { text: '—' };
}
