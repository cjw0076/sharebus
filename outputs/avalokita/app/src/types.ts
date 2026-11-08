import { copy } from './mainCopy';

export interface Participant {
  id: string; provider: string; name: string; role: string; status: string;
  attachment?: string; verified?: boolean; capabilities?: string[]; latest_message?: string;
  native_session_bound?: boolean;
  transport?: string; identity_state?: 'pending' | 'observed';
  readiness?: { state: string; can_dispatch: boolean; route?: string | null; reason?: string };
}
export interface HostedQuestion { id?: string; header?: string; question: string; options?: Array<{ label: string; description?: string }>; isOther?: boolean; isSecret?: boolean; multiSelect?: boolean }
export interface HostedApproval {
  agent_id: string; approval_id: string; provider: string; method: string; summary?: string; questions?: HostedQuestion[];
  details?: { title?: string; summary?: string; questions?: HostedQuestion[]; command?: string; cwd?: string; paths?: string[]; tool_call?: { title?: string; kind?: string }; options?: Array<{ optionId: string; kind: string; name?: string }> }; can_approve?: boolean; reason?: string; job_id: string; root_id?: string; native_turn_id?: string | null;
}
export interface HostedState {
  enabled: boolean; providers: string[]; supported: string[]; state: string; reason?: string;
  sessions: Array<{ agent_id: string; provider: string; transport: 'hosted_cli'; identity_state: 'pending' | 'observed'; native_session_id: string | null; state: string; delivery_state: string; approval_pending: boolean }>;
  approvals: HostedApproval[];
}
export interface Message {
  id: string; seq?: number; actor: string; provider?: string; body: string;
  kind?: string; created_at: string | number; delivery?: string;
  source_event_id?: string; question_id?: string; question_pending?: boolean; root_id?: string; recipient?: string | null;
}
export interface TeamEnvelope { team: Record<string, unknown>; revision?: string | number; runtime_enforced?: boolean; [key: string]: unknown }
export interface DispatchOutcome { agent_id?: string; job_id?: string; state?: string; status?: string; delivery_verified?: boolean; reason?: string }
export interface Dispatch {
  state?: string; status?: string; durable_accepted?: boolean; root_id?: string; event_id?: string;
  participants?: string[]; outcomes?: DispatchOutcome[]; skipped?: DispatchOutcome[]; reason?: string;
  delivery_verified?: boolean; pending?: boolean;
}
export interface PromptOutcome { goal?: { id?: string }; dispatch?: Dispatch; native_delivery?: Array<{ state?: string }> }
export interface Goal { id: string; prompt?: string; state?: string; created_at?: number; deadline?: number; budget?: number; used?: number }
export interface PublicJob { id: string; event_id?: string; agent_id: string; root_id?: string; state: string; created_at?: number; started_at?: number | null; finished_at?: number | null; parent_job_id?: string | null; recipient?: string | null; sender?: string | null }
export interface PublicLease { id: string; actor: string; resources: string[]; state: string; root_id?: string; expires_at?: number; fence?: number }
export interface PublicContext { id: string; actor: string; label: string; generation?: number; total_bytes?: number; created_at?: number; root_id?: string; files?: Array<{ path: string; sha256?: string; bytes?: number }> }
export interface PublicArtifact { id: string; owner: string; filename: string; sha256?: string; size?: number; created_at?: number; root_id?: string; provider?: string; preview_supported?: boolean }
export interface DiscoveryStatus { state: 'pending' | 'ready' | 'error' | 'disabled'; observed_at?: number | null; last_success_at?: number | null; stale?: boolean; refreshing?: boolean; error_code?: string | null }
export interface DeviceCandidate {
  provider?: string; name?: string; installed?: boolean; running?: boolean; state?: string; notes?: string | string[];
  candidate_count?: number; process_count?: number;
  processes?: Array<{ pid: number; parent_pid?: number; name?: string; executable?: string | null; started_at?: number | null; identity?: string | null; owner_state?: string; provider_verified?: boolean; group_id?: string | null; helper?: boolean }>;
  native_sessions?: Array<{ native_session_id: string; cwd?: string; pid?: number; status?: string; kind?: string; requires_attach?: boolean; capability_verified?: boolean }>;
  session_scan?: { state: string; observed_at?: number; error_code?: string | null };
  [key: string]: unknown;
}
export interface WorkspaceEntry { id: string; name: string; path: string; lastOpenedAt?: number | string | null; availability: 'available' | 'missing' | 'unavailable' }
export interface WorkspaceList { activeId: string; generation: number; entries: WorkspaceEntry[] }
export interface InventorySnapshot {
  schema?: string; state: string; bounded?: boolean; observed_at?: number | null;
  refreshes?: number; complete: boolean; records?: number; evidence_samples?: number;
  sources?: Array<{ source: string; status: string; records?: number; complete?: boolean }>;
  learning?: { state: string; heads?: number; revision?: number; checkpoint?: string; recovery?: string | null };
  error_code?: string | null; worker_alive?: boolean; native_input_created?: boolean; model_api_calls?: number;
}
export interface SummaryState {
  workspace: RoomState['workspace']; observed_at?: number; inventory?: InventorySnapshot;
}
export interface DesktopWorkspaceAPI {
  listWorkspaces(): Promise<WorkspaceList>;
  chooseWorkspace(): Promise<{ cancelled: boolean; entry?: WorkspaceEntry }>;
  activateWorkspace(id: string, context: { activeId: string; generation: number }): Promise<WorkspaceList>;
  onWorkspaceActivationRequested?(listener: (id: string) => void | Promise<void>): () => void;
}
declare global { interface Window { avalokitaDesktop?: DesktopWorkspaceAPI } }
export interface RoomState {
  workspace: { path?: string; id?: string; name?: string };
  connection: { state: string; message?: string; paused?: boolean; [key: string]: unknown };
  participants: Participant[]; messages: Message[]; goals?: Goal[]; leases?: PublicLease[];
  jobs?: PublicJob[]; contexts?: PublicContext[]; artifacts?: PublicArtifact[];
  discovery_status?: DiscoveryStatus;
  inspector?: { limits?: Record<string, number>; totals?: Record<string, number>; truncated?: boolean | Record<string, boolean>; status?: string; observed_at?: number; error_code?: string | null };
  team?: TeamEnvelope | Record<string, unknown>;
  dispatches?: Record<string, Dispatch>;
  discovery?: DeviceCandidate[];
}
export interface Seat { key: string; provider: string; label: string; participant?: Participant; installed: boolean }
export const providerKey = (value = '') => /claude|anthropic/i.test(value) ? 'claude' : /antigravity|gemini|google/i.test(value) ? 'gemini' : /codex|chatgpt|openai/i.test(value) ? 'codex' : value.toLowerCase();
export const isAttached = (p?: Participant) => !!p && p.native_session_bound === true && (['native', 'attached', 'connected', 'native_attached', 'native_verified', 'external_attached'].includes((p.attachment || '').toLowerCase()) || p.transport === 'hosted_cli');
export function isWorking(p?: Participant) { return !!p && /^(working|running|busy|thinking)$/i.test(p.status) && isAttached(p) && p.readiness?.state === 'busy' && p.readiness.reason === 'native_session_busy'; }
export const transportLabel = (p: Participant) => copy(p.transport === 'hosted_cli' ? 'transport_hosted_cli' : 'transport_native_session');
export function deliveryLabel(status = '') {
  const labels: Record<string, Parameters<typeof copy>[0]> = {
    completed: 'delivery_completed', partially_completed: 'delivery_partially_completed', working: 'delivery_working', running: 'delivery_working',
    queued: 'delivery_queued', claimed: 'delivery_claimed', native_accepted: 'delivery_native_accepted', awaiting_native_reply: 'delivery_awaiting_native_reply',
    waiting: 'delivery_waiting', waiting_user: 'delivery_waiting_user', waiting_for_user: 'delivery_waiting_user',
    paused: 'delivery_paused', avalokita_paused: 'delivery_paused', pending_capability: 'delivery_pending_capability',
    delivery_unknown: 'delivery_unknown', unknown: 'delivery_state_unknown', pending_review: 'delivery_pending_review', unavailable: 'delivery_unavailable', failed: 'delivery_failed',
    avalokita_native_pending: 'delivery_native_pending', legacy_owned: 'delivery_legacy_owned', busy: 'delivery_busy',
  };
  return copy(labels[status] || 'delivery_unconfirmed_fallback');
}
export function statusLabel(p?: Participant, installed = false) {
  if (!p) return copy(installed ? 'seat_status_candidate_installed' : 'seat_status_waiting_connection');
  if (p.transport === 'hosted_cli' && p.status === 'idle' && !p.native_session_bound && p.identity_state !== 'observed') return copy('seat_status_hosted_first_wait');
  if (['awaiting_native_reply', 'waiting_user', 'waiting_for_user', 'queued', 'claimed', 'avalokita_native_pending', 'delivery_unknown', 'pending_capability'].includes(p.status)) return deliveryLabel(p.status);
  if (/offline|stale|unknown|disconnected/i.test(p.status)) return copy('seat_status_unconfirmed');
  if (/paused|suspended/i.test(p.status)) return copy('seat_status_paused');
  if (/wait|needs_human/i.test(p.status)) return copy('seat_status_needs_human');
  if (isWorking(p)) return copy('seat_status_working');
  if (p.readiness?.state === 'unavailable') return copy('seat_status_registered_unavailable');
  if (p.verified && isAttached(p)) return copy('seat_status_verified');
  if (isAttached(p)) return copy('seat_status_attached_unverified');
  return copy('seat_status_registered_unconfirmed');
}
export function readableMessage(body: string) {
  try {
    const value: unknown = JSON.parse(body);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return body;
    const selected = ['answer', 'proposal', 'message'].map(key => (value as Record<string, unknown>)[key])
      .filter((text): text is string => typeof text === 'string' && !!text.trim());
    return selected.length ? [...new Set(selected)].join('\n\n') : body;
  } catch { return body; }
}
export function messageKind(kind = '') {
  const keys: Record<string, Parameters<typeof copy>[0]> = { proposal: 'message_kind_proposal', claim: 'message_kind_claim', progress: 'message_kind_progress', review: 'message_kind_review', handoff: 'message_kind_handoff', result: 'message_kind_result', question: 'message_kind_question', status: 'message_kind_status', intent: 'message_kind_intent', message: 'message_kind_message', peer_message: 'message_kind_message', example: 'message_kind_example' };
  return copy(keys[kind] || 'message_kind_message');
}
export function messageTime(value: string | number) {
  if (!value) return copy('message_kind_example');
  const numeric = typeof value === 'number' ? value : /^\d+(\.\d+)?$/.test(value) ? Number(value) : null;
  const date = new Date(numeric === null ? value : numeric < 100_000_000_000 ? numeric * 1000 : numeric);
  return Number.isNaN(date.getTime()) ? copy('message_time_unconfirmed') : date.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
}
export function seatsFor(state: RoomState): Seat[] {
  const seats: Seat[] = state.participants.map(p => ({ key: p.id, provider: providerKey(p.provider), label: p.name || p.provider, participant: p, installed: true }));
  const recent = [...new Set([...state.messages].reverse().map(message => message.actor))];
  // Only joined participants occupy seats. Discovery remains a separate observation.
  return seats.length <= 6 ? seats : seats.sort((a, b) => (recent.indexOf(a.key) < 0 ? Infinity : recent.indexOf(a.key)) - (recent.indexOf(b.key) < 0 ? Infinity : recent.indexOf(b.key))).slice(0, 6);
}
export const EMPTY_STATE: RoomState = { workspace: {}, connection: { state: 'unavailable' }, participants: [], messages: [], discovery: [] };
