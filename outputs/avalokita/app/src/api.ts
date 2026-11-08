import type { SummaryState } from './types';
let sessionReady: Promise<void> | null = null;
const HEADERS = { 'X-Avalokita-Request': '1' };
export class ApiError extends Error { status: number; constructor(message: string, status: number) { super(message); this.status = status; } }
async function decode(response: Response) {
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(body?.error?.message || body?.message || (typeof body?.error === 'string' ? body.error : `요청을 처리하지 못했어요 (${response.status})`), response.status);
  return body;
}
async function ensureSession() {
  if (!sessionReady) sessionReady = fetch('/api/session', { headers: HEADERS, credentials: 'same-origin', cache: 'no-store' }).then(decode).then(() => undefined).catch(error => { sessionReady = null; throw error; });
  await sessionReady;
}
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  await ensureSession();
  const response = await fetch(`/api${path}`, { ...init, credentials: 'same-origin', cache: 'no-store', headers: { ...HEADERS, ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers } });
  if (response.status === 401 || response.status === 403) sessionReady = null;
  return decode(response);
}

// Observation-only cached projection; this endpoint never initiates discovery or a model call.
export const readSummary = () => api<SummaryState>('/summary');
