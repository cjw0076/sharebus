import { useCallback, useEffect, useRef, useState } from 'react';
import CouncilScene from './CouncilScene';
import type { Anchor } from './CouncilScene';
import { api, readSummary } from './api';
import { copy } from './mainCopy';
import { DEMO_STATE } from './demo';
import { HostedApprovals, HostedSettings, WorkspaceRail, WorkInspector } from './ADEPanels';
import { deliveryLabel, EMPTY_STATE, isWorking, messageKind, messageTime, readableMessage, seatsFor, statusLabel } from './types';
import type { HostedApproval, HostedState, PromptOutcome, RoomState, SummaryState, TeamEnvelope, WorkspaceList } from './types';

const demo = new URLSearchParams(location.search).get('demo') === '1';
// Native/backend errors may contain technical locators. Keep the normal UI in approved copy.
const errorText = (_error: unknown) => copy('delivery_failed');
type Profile = { display_model?: string; model_id?: string | null; effort?: string | null; [key: string]: unknown };
type Team = { providers?: Record<string, Record<string, Profile>>; roles?: Record<string, { label?: string; preferred_provider?: string; [key: string]: unknown }>; [key: string]: unknown };
type Reply = { id: string; label: string; hosted?: { agentId: string; index: number; answers: Record<string, unknown> } } | null;
type Pending = { text: string; key: string; replyTo: string | null } | null;
type Draft = { text: string; reply: Reply; pending: Pending };
function promptNotice(result: PromptOutcome) {
  const dispatch = result.dispatch;
  if (!dispatch) return copy('prompt_outcome_unconfirmed');
  const statuses = dispatch.outcomes?.map(outcome => outcome.status || outcome.state || '') || [];
  const status = dispatch.status || (result.native_delivery?.some(delivery => delivery.state === 'delivery_unknown') ? 'delivery_unknown' : dispatch.state);
  const label = deliveryLabel(status);
  const unavailable = dispatch.skipped?.length || 0;
  const accepted = dispatch.durable_accepted === true || (dispatch.durable_accepted !== false && !!result.goal?.id && !!dispatch.root_id);
  return [copy(accepted ? 'prompt_outcome_accepted_prefix' : 'prompt_outcome_pending_prefix', { status: label }), statuses.length > 1 ? copy('prompt_outcome_response_ratio', { completed: statuses.filter(value => value === 'completed').length, total: statuses.length }) : '', unavailable ? copy('prompt_outcome_skipped_sessions', { count: unavailable }) : ''].filter(Boolean).join(' · ');
}
function MessageBody({ body, human }: { body: string; human: boolean }) {
  const text = human ? body : readableMessage(body);
  return <><p>{text}</p>{text !== body && <details className="original-message"><summary>{copy('conversation_view_raw_json')}</summary><pre>{body}</pre></details>}</>;
}
function Icon({ name }: { name: 'pause' | 'play' | 'history' | 'settings' | 'close' | 'send' | 'rail' | 'inspect' }) {
  const paths = { pause: 'M8 5v14M16 5v14', play: 'm8 5 11 7-11 7Z', history: 'M4 11a8 8 0 1 1 2 7M4 5v6h6M12 8v5l3 2', settings: 'M4 7h16M4 17h16M8 4v6M16 14v6', close: 'm6 6 12 12M18 6 6 18', send: 'M12 19V5m-6 6 6-6 6 6', rail: 'M4 5h16v14H4ZM9 5v14', inspect: 'M4 5h16v14H4ZM15 5v14M7 9h4M7 13h4' };
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}
export default function App() {
  const [state, setState] = useState<RoomState>(demo ? DEMO_STATE : EMPTY_STATE);
  const [online, setOnline] = useState(demo);
  const [draft, setDraft] = useState('');
  const [draftSaved, setDraftSaved] = useState<'saving' | 'saved' | 'local'>('local');
  const [replyTo, setReplyTo] = useState<Reply>(null);
  const [sending, setSending] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [drawer, setDrawer] = useState<'history' | 'settings' | null>(null);
  const [conversationOpen, setConversationOpen] = useState(false);
  const [interactionRevision, setInteractionRevision] = useState(0);
  const [summary, setSummary] = useState<SummaryState | null>(null);
  const [summaryOnline, setSummaryOnline] = useState(false);
  const [anchors, setAnchors] = useState<Record<string, Anchor>>({});
  const [reducedMotion, setReducedMotion] = useState(matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [team, setTeam] = useState<Team | null>(null);
  const [teamEnvelope, setTeamEnvelope] = useState<TeamEnvelope | null>(null);
  const [teamBusy, setTeamBusy] = useState(false);
  const [teamNotice, setTeamNotice] = useState('');
  const [hosted, setHosted] = useState<HostedState | null>(null);
  const [hostedSelection, setHostedSelection] = useState<string[] | null>(null);
  const [hostedOnline, setHostedOnline] = useState(false);
  const [hostedBusy, setHostedBusy] = useState(false);
  const [hostedNotice, setHostedNotice] = useState('');
  const [hostedBlockedReplies, setHostedBlockedReplies] = useState<string[]>([]);
  const [railOpen, setRailOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<'activity' | 'work' | 'outputs'>('activity');
  const [selectedSession, setSelectedSession] = useState<string | null>(null);
  const [selectedGoal, setSelectedGoal] = useState<string | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceList | null>(null);
  const [workspaceNotice, setWorkspaceNotice] = useState('');
  const [switching, setSwitching] = useState(false);
  const [narrow, setNarrow] = useState(matchMedia('(max-width: 1099px)').matches);
  const [inspectorFloating, setInspectorFloating] = useState(matchMedia('(max-width: 1419px)').matches);
  const composer = useRef<HTMLTextAreaElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const followConversation = useRef(true);
  const composing = useRef(false);
  const pending = useRef<Pending>(null);
  const draftValue = useRef(draft); draftValue.current = draft;
  const replyValue = useRef(replyTo); replyValue.current = replyTo;
  const draftKey = useRef('');
  const draftRevision = useRef(0);
  const inputRevision = useRef(0);
  const draftTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draftSaves = useRef<Promise<void>>(Promise.resolve());
  const queuedDraft = useRef<{ key: string; saved: Draft; revision: number } | null>(null);
  const draftSaveFailed = useRef(false);
  const switchingRef = useRef(false);
  const lifetime = useRef(0);
  const refreshSequence = useRef(0);
  const hostedRead = useRef(0);
  const hostedMutation = useRef(false);
  const fixedWorkspace = useRef<string | null>(null);
  const workspaceRead = useRef(0);
  const workspaceActivation = useRef<(id: string) => Promise<void>>(async () => undefined);
  const externalNavigationReady = useRef<(() => void) | null>(null);
  const noticeContext = useRef(0);
  const submittedNotice = useRef<{ goalId: string; context: number } | null>(null);
  const seats = seatsFor(state);
  const paused = !!state.connection.paused;
  const busConnected = demo || state.connection.state === 'connected';
  const working = online && busConnected && !paused ? state.participants.filter(isWorking).length : 0;
  const workspaceKey = state.workspace.id || state.workspace.path;
  const closeRail = () => { setRailOpen(false); requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('.rail-toggle')?.focus()); };
  const closeInspector = () => { setInspectorOpen(false); requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('.inspector-toggle')?.focus()); };
  const releaseExternalNavigation = () => { externalNavigationReady.current?.(); externalNavigationReady.current = null; };
  const commitDraft = () => {
    const queued = queuedDraft.current;
    if (!queued) return draftSaves.current;
    queuedDraft.current = null;
    const { key, saved, revision } = queued;
    const generation = lifetime.current;
    draftSaves.current = draftSaves.current.catch(() => undefined)
      .then(() => api<{ updated_at?: number }>('/draft', { method: 'POST', body: JSON.stringify(saved) }))
      .then(result => {
        draftSaveFailed.current = false;
        if (generation === lifetime.current && key === draftKey.current && revision === draftRevision.current) {
          setDraftSaved('saved');
          try { localStorage.setItem(key, JSON.stringify({ ...saved, saved_at: result.updated_at || Date.now() / 1000 })); } catch { /* Private workspace copy is durable. */ }
        }
      }).catch(error => {
        draftSaveFailed.current = true;
        if (generation === lifetime.current && revision === draftRevision.current) setDraftSaved('local');
        throw error;
      });
    void draftSaves.current.catch(() => undefined);
    return draftSaves.current;
  };
  const saveDraft = (text: string, reply: Reply, request: Pending = pending.current) => {
    if (!draftKey.current || demo) return;
    const saved: Draft = { text, reply, pending: request };
    const key = draftKey.current;
    try { localStorage.setItem(key, JSON.stringify({ ...saved, saved_at: Date.now() / 1000 })); } catch { /* The private workspace save below also works without browser storage. */ }
    const revision = ++draftRevision.current;
    if (draftTimer.current) clearTimeout(draftTimer.current);
    setDraftSaved('saving');
    queuedDraft.current = { key, saved, revision };
    draftTimer.current = setTimeout(() => {
      draftTimer.current = null;
      void commitDraft().catch(() => undefined);
    }, 250);
  };
  const updateDraft = (text: string) => { if (switchingRef.current) return; ++inputRevision.current; ++noticeContext.current; submittedNotice.current = null; setNotice(''); draftValue.current = text; setDraft(text); saveDraft(text, replyValue.current); };
  const chooseReply = (reply: Reply) => { if (switchingRef.current) return; ++inputRevision.current; ++noticeContext.current; submittedNotice.current = null; setNotice(''); replyValue.current = reply; setReplyTo(reply); saveDraft(draftValue.current, reply); };
  const refresh = useCallback(async () => {
    if (demo) return;
    const sequence = ++refreshSequence.current, generation = lifetime.current;
    const next = await api<RoomState>('/state');
    if (sequence !== refreshSequence.current || generation !== lifetime.current || document.hidden) return;
    const path = next.workspace.path || null;
    if (fixedWorkspace.current && path !== fixedWorkspace.current) throw new Error(copy('workspace_cross_response_guard'));
    if (path) fixedWorkspace.current = path;
    setState({ ...next, participants: next.participants || [], messages: next.messages || [] }); setOnline(true);
  }, []);
  const refreshHosted = useCallback(async () => {
    if (demo || hostedMutation.current) return;
    const request = ++hostedRead.current, generation = lifetime.current, path = fixedWorkspace.current;
    try {
      const result = await api<HostedState>('/hosted');
      if (request !== hostedRead.current || generation !== lifetime.current || path !== fixedWorkspace.current || document.hidden) return;
      setHosted(result); setHostedOnline(true);
    } catch {
      if (request === hostedRead.current && generation === lifetime.current) setHostedOnline(false);
    }
  }, []);
  useEffect(() => {
    if (demo || !workspaceKey) return;
    let stopped = false, sequence = 0, timer: ReturnType<typeof setTimeout> | undefined;
    setSummary(null); setSummaryOnline(false);
    const poll = async () => {
      if (stopped || document.hidden) return;
      const request = ++sequence, generation = lifetime.current, path = fixedWorkspace.current;
      try {
        const result = await readSummary();
        if (!stopped && request === sequence && generation === lifetime.current && !document.hidden && result.workspace.path === path) {
          setSummary(result); setSummaryOnline(true);
        }
      } catch { if (!stopped && request === sequence) setSummaryOnline(false); }
      finally { if (!stopped && request === sequence && !document.hidden) timer = setTimeout(poll, 22_000); }
    };
    const visible = () => { if (timer) clearTimeout(timer); ++sequence; if (!document.hidden) void poll(); };
    document.addEventListener('visibilitychange', visible); void poll();
    return () => { stopped = true; ++sequence; if (timer) clearTimeout(timer); document.removeEventListener('visibilitychange', visible); };
  }, [workspaceKey]);
  useEffect(() => {
    if (demo || !workspaceKey) return;
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      if (stopped || document.hidden) return;
      await refreshHosted();
      if (!stopped && !document.hidden) timer = setTimeout(poll, 2200);
    };
    const visible = () => { if (timer) clearTimeout(timer); ++hostedRead.current; if (!document.hidden) void poll(); };
    document.addEventListener('visibilitychange', visible); void poll();
    return () => { stopped = true; ++hostedRead.current; if (timer) clearTimeout(timer); document.removeEventListener('visibilitychange', visible); };
  }, [refreshHosted, workspaceKey]);
  useEffect(() => { if (drawer !== 'settings') setHostedSelection(null); }, [drawer]);
  useEffect(() => {
    if (demo) return;
    let stopped = false, inFlight = false, timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      if (stopped || document.hidden || inFlight) return;
      inFlight = true;
      try { await refresh(); } catch { if (!stopped && !document.hidden) setOnline(false); }
      finally { inFlight = false; if (!stopped && !document.hidden) timer = setTimeout(poll, 2200); }
    };
    const visibility = () => { if (timer) clearTimeout(timer); ++refreshSequence.current; if (!document.hidden) void poll(); };
    document.addEventListener('visibilitychange', visibility);
    void poll(); return () => { stopped = true; ++refreshSequence.current; if (timer) clearTimeout(timer); document.removeEventListener('visibilitychange', visibility); };
  }, [refresh]);
  const loadWorkspaces = useCallback(async () => {
    if (!window.avalokitaDesktop || demo) return;
    const request = ++workspaceRead.current, generation = lifetime.current;
    try { const result = await window.avalokitaDesktop.listWorkspaces(); if (request === workspaceRead.current && generation === lifetime.current) setWorkspaces(result); }
    catch (error) { if (request === workspaceRead.current && generation === lifetime.current) setWorkspaceNotice(errorText(error)); }
  }, []);
  useEffect(() => {
    void loadWorkspaces();
    const visible = () => { if (!document.hidden) void loadWorkspaces(); };
    document.addEventListener('visibilitychange', visible);
    return () => document.removeEventListener('visibilitychange', visible);
  }, [loadWorkspaces]);
  useEffect(() => () => { ++lifetime.current; ++workspaceRead.current; releaseExternalNavigation(); if (draftTimer.current) clearTimeout(draftTimer.current); }, []);
  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      if (dialog.current?.open) return;
      if (event.key === 'Escape') { if (inspectorOpen) closeInspector(); else if (railOpen) closeRail(); }
      const overlay = inspectorFloating && inspectorOpen ? '.work-inspector' : narrow && railOpen ? '.workspace-rail' : null;
      if (event.key !== 'Tab' || !overlay) return;
      const controls = [...document.querySelectorAll<HTMLElement>(`${overlay} button:not(:disabled), ${overlay} select, ${overlay} summary`)].filter(element => element.offsetParent !== null);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first && last) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last && first) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', keyboard); return () => document.removeEventListener('keydown', keyboard);
  }, [narrow, railOpen, inspectorFloating, inspectorOpen]);
  useEffect(() => {
    const railMedia = matchMedia('(max-width: 1099px)'), inspectorMedia = matchMedia('(max-width: 1419px)');
    const resize = () => { setNarrow(railMedia.matches); setInspectorFloating(inspectorMedia.matches); if (railMedia.matches) setRailOpen(false); };
    railMedia.addEventListener('change', resize); inspectorMedia.addEventListener('change', resize);
    return () => { railMedia.removeEventListener('change', resize); inspectorMedia.removeEventListener('change', resize); };
  }, []);
  useEffect(() => {
    if (narrow && railOpen) document.querySelector<HTMLButtonElement>('.rail-close')?.focus();
    else if (inspectorFloating && inspectorOpen) document.querySelector<HTMLButtonElement>('.inspector-header button')?.focus();
  }, [narrow, railOpen, inspectorFloating, inspectorOpen]);
  const openWorkspace = async (id?: string) => {
    const desktop = window.avalokitaDesktop;
    if (!desktop || demo || switchingRef.current || composing.current) return;
    if (id && workspaces?.entries.find(entry => entry.id === id)?.path === state.workspace.path) return;
    switchingRef.current = true; setSwitching(true); setWorkspaceNotice('');
    const generation = lifetime.current;
    try {
      if (!draftKey.current && draftValue.current) throw new Error(copy('workspace_switch_draft_not_ready'));
      const snapshot = await desktop.listWorkspaces();
      let target = id;
      if (!target) { const result = await desktop.chooseWorkspace(); if (result.cancelled) return; target = result.entry?.id; }
      if (!target) throw new Error(copy('workspace_switch_folder_unconfirmed'));
      if (draftTimer.current) { clearTimeout(draftTimer.current); draftTimer.current = null; }
      if (draftSaveFailed.current && !queuedDraft.current) saveDraft(draftValue.current, replyValue.current);
      if (draftTimer.current) { clearTimeout(draftTimer.current); draftTimer.current = null; }
      await commitDraft();
      if (generation !== lifetime.current) return;
      const result = await desktop.activateWorkspace(target, { activeId: snapshot.activeId, generation: snapshot.generation });
      if (generation === lifetime.current) { ++workspaceRead.current; setWorkspaces(result); setRailOpen(false); }
    } catch (error) {
      if (generation === lifetime.current) { setWorkspaceNotice(copy('workspace_switch_error_preserved', { error: errorText(error) })); void loadWorkspaces(); }
    } finally { switchingRef.current = false; if (generation === lifetime.current) setSwitching(false); if (!composing.current) releaseExternalNavigation(); }
  };
  workspaceActivation.current = openWorkspace;
  useEffect(() => {
    if (demo) return;
    return window.avalokitaDesktop?.onWorkspaceActivationRequested?.(async id => {
      if (typeof id !== 'string' || !id.length || id.length > 128) return;
      const generation = lifetime.current;
      try {
        if (composing.current || switchingRef.current) {
          setWorkspaceNotice(composing.current ? copy('workspace_external_wait_composing') : copy('workspace_external_wait_switching'));
          await new Promise<void>((resolve, reject) => {
            const ready = () => { clearTimeout(timeout); resolve(); };
            const timeout = setTimeout(() => { if (externalNavigationReady.current === ready) externalNavigationReady.current = null; reject(new Error(copy('workspace_external_timeout_preserved'))); }, 30_000);
            externalNavigationReady.current = ready;
          });
          if (generation !== lifetime.current || document.hidden) return;
        }
        await workspaceActivation.current(id);
      } catch (error) { if (generation === lifetime.current) setWorkspaceNotice(errorText(error)); }
    });
  }, []);
  useEffect(() => {
    if (!workspaceKey || demo) return;
    const key = `avalokita:draft:v1:${encodeURIComponent(workspaceKey)}`;
    if (draftKey.current === key) return;
    const firstWorkspace = !draftKey.current;
    draftKey.current = key;
    let localDraft: Draft | null = null, localSavedAt = 0;
    if (firstWorkspace && draftValue.current) { saveDraft(draftValue.current, replyValue.current); return; }
    try {
      const saved = JSON.parse(localStorage.getItem(key) || 'null');
      if (typeof saved?.text === 'string') {
        draftValue.current = saved.text; setDraft(saved.text);
        const reply = typeof saved.reply?.id === 'string' && typeof saved.reply?.label === 'string' ? saved.reply as Reply : null;
        replyValue.current = reply; setReplyTo(reply);
        pending.current = saved.pending?.text === saved.text.trim() && typeof saved.pending?.key === 'string'
          && (typeof saved.pending.replyTo === 'string' || saved.pending.replyTo === null) ? saved.pending : null;
        localDraft = { text: saved.text, reply, pending: pending.current };
        localSavedAt = typeof saved.saved_at === 'number' ? saved.saved_at : 0;
      }
    } catch { /* Invalid or unavailable local storage never replaces the current draft. */ }
    const revision = draftRevision.current;
    void api<{ draft: Draft | null; updated_at?: number }>('/draft').then(result => {
        const saved = result.draft;
        if (draftKey.current !== key || revision !== draftRevision.current) return;
        if (localDraft && (typeof saved?.text !== 'string' || localSavedAt >= (result.updated_at || 0))) {
          saveDraft(localDraft.text, localDraft.reply, localDraft.pending); return;
        }
        if (typeof saved?.text !== 'string') return;
        const reply = typeof saved.reply?.id === 'string' && typeof saved.reply?.label === 'string' ? saved.reply : null;
        pending.current = saved.pending?.text === saved.text.trim() && typeof saved.pending?.key === 'string'
          && (typeof saved.pending.replyTo === 'string' || saved.pending.replyTo === null) ? saved.pending : null;
        draftValue.current = saved.text; setDraft(saved.text); replyValue.current = reply; setReplyTo(reply);
        try { localStorage.setItem(key, JSON.stringify({ text: saved.text, reply, pending: pending.current, saved_at: result.updated_at || Date.now() / 1000 })); } catch { /* The workspace copy is already durable. */ }
        setDraftSaved('saved');
      }).catch(() => setDraftSaved('local'));
  }, [workspaceKey]);
  useEffect(() => {
    if (followConversation.current && transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
  }, [state.messages.length, state.messages.at(-1)?.body]);
  useEffect(() => {
    const submitted = submittedNotice.current;
    if (!submitted || submitted.context !== noticeContext.current) return;
    const dispatch = state.dispatches?.[submitted.goalId];
    if (dispatch) setNotice(promptNotice({ goal: { id: submitted.goalId }, dispatch: { ...dispatch, durable_accepted: true } }));
  }, [state.dispatches]);
  useEffect(() => { const media = matchMedia('(prefers-reduced-motion: reduce)'); const update = () => setReducedMotion(media.matches); media.addEventListener('change', update); return () => media.removeEventListener('change', update); }, []);
  useEffect(() => { if (drawer) dialog.current?.showModal(); else dialog.current?.close(); }, [drawer]);
  useEffect(() => {
    if (drawer !== 'settings' || demo) return;
    let active = true; setTeamNotice(copy('settings_loading_notice'));
    api<TeamEnvelope>('/team').then(data => { if (active) { setTeamEnvelope(data); setTeam(data.team as Team); setTeamNotice(''); } }).catch(error => { if (active) setTeamNotice(errorText(error)); });
    return () => { active = false; };
  }, [drawer]);
  const send = async () => {
    const text = draft.trim(); if (!text || sending || hostedBusy || paused || !online || switchingRef.current) return;
    if (demo) { setNotice(copy('demo_send_blocked')); return; }
    if (replyTo?.hosted) {
      const approval = hosted?.approvals.find(item => item.approval_id === replyTo.id && item.agent_id === replyTo.hosted?.agentId);
      if (!approval || !hostedOnline) { setNotice(copy('reply_question_missing_or_offline')); return; }
      if (approval.can_approve !== true) { setNotice(copy('reply_question_blocked_incomplete')); return; }
      const questions = approval.questions || approval.details?.questions || [];
      if (approval.method === 'mcpServer/elicitation/request') {
        try {
          const content: unknown = JSON.parse(text);
          if (content !== null && (typeof content !== 'object' || Array.isArray(content))) throw new Error();
          await answerHosted(approval, { action: 'accept', content });
        } catch { setNotice(copy('reply_question_elicitation_json_error')); }
        return;
      }
      if (approval.method !== 'item/tool/requestUserInput' && !(approval.provider === 'claude' && approval.method === 'can_use_tool' && questions.length)) { setNotice(copy('reply_question_unsupported_format')); return; }
      const question = questions[replyTo.hosted.index];
      if (!question || question.isSecret || (!question.id && approval.provider !== 'claude')) { setNotice(copy('reply_question_unsupported_format')); return; }
      const answers = { ...replyTo.hosted.answers, [approval.provider === 'claude' ? question.question : question.id!]: approval.provider === 'claude' ? text : { answers: [text] } };
      if (replyTo.hosted.index + 1 < questions.length) {
        const reply = { ...replyTo, hosted: { ...replyTo.hosted, index: replyTo.hosted.index + 1, answers } };
        draftValue.current = ''; setDraft(''); replyValue.current = reply; setReplyTo(reply); saveDraft('', reply); setNotice(copy('reply_question_next_notice'));
      } else await answerHosted(approval, { ...(approval.provider === 'claude' ? { behavior: 'allow' } : {}), answers });
      return;
    }
    if (!pending.current || pending.current.text !== text || pending.current.replyTo !== (replyTo?.id ?? null)) pending.current = { text, key: crypto.randomUUID(), replyTo: replyTo?.id ?? null };
    const request = pending.current;
    const attempt = Object.freeze({ request, revision: inputRevision.current, rawText: draftValue.current, reply: replyValue.current });
    const context = ++noticeContext.current;
    const generation = lifetime.current;
    submittedNotice.current = null;
    saveDraft(draftValue.current, replyValue.current, request);
    setSending(true); setNotice('');
    try {
      const result = await api<PromptOutcome>('/prompt', { method: 'POST', body: JSON.stringify({ text, idempotency_key: request.key, ...(replyTo ? { reply_to: replyTo.id } : {}) }) });
      if (generation !== lifetime.current) return;
      const accepted = result.dispatch?.durable_accepted === true || (result.dispatch?.durable_accepted !== false && !!result.goal?.id && !!result.dispatch?.root_id);
      if (accepted) {
        const unchanged = inputRevision.current === attempt.revision && draftValue.current === attempt.rawText && replyValue.current === attempt.reply;
        const nextDraft = unchanged ? '' : draftValue.current;
        const nextReply = unchanged ? null : replyValue.current;
        if (pending.current === attempt.request) pending.current = null;
        draftValue.current = nextDraft; setDraft(nextDraft);
        replyValue.current = nextReply; setReplyTo(nextReply);
        saveDraft(nextDraft, nextReply, null);
        followConversation.current = true;
      }
      if (context === noticeContext.current) {
        if (accepted && result.goal?.id) submittedNotice.current = { goalId: result.goal.id, context };
        setNotice(promptNotice(result));
      }
      await refresh().catch(() => setOnline(false));
    } catch (error) { if (generation === lifetime.current && context === noticeContext.current) { submittedNotice.current = null; setNotice(copy('prompt_send_error_draft_kept', { error: errorText(error) })); } }
    finally { if (generation === lifetime.current) { setSending(false); if (!document.hidden && !switchingRef.current) composer.current?.focus(); } }
  };
  const control = async () => {
    if (demo || controlBusy || switchingRef.current) return; setControlBusy(true);
    const generation = lifetime.current;
    const context = ++noticeContext.current; submittedNotice.current = null; setNotice('');
    try { await api('/control', { method: 'POST', body: JSON.stringify({ action: paused ? 'resume' : 'pause' }) }); await refresh(); if (generation === lifetime.current && context === noticeContext.current) setNotice(paused ? copy('control_resume_notice') : copy('control_pause_notice')); }
    catch (error) { if (generation === lifetime.current && context === noticeContext.current) setNotice(errorText(error)); } finally { if (generation === lifetime.current) setControlBusy(false); }
  };
  const saveTeam = async () => {
    if (!team || !teamEnvelope) return; setTeamBusy(true); setTeamNotice('');
    try { const saved = await api<TeamEnvelope>('/team', { method: 'POST', body: JSON.stringify({ team, expected_revision: teamEnvelope.revision ?? null }) }); setTeamEnvelope(saved); setTeam((saved.team || team) as Team); setTeamNotice(copy('settings_team_saved_notice')); }
    catch (error) { setTeamNotice(errorText(error)); } finally { setTeamBusy(false); }
  };
  const saveHosted = async () => {
    if (demo || !hosted || !hostedOnline || hostedMutation.current || switchingRef.current) return;
    hostedMutation.current = true; ++hostedRead.current; setHostedBusy(true); setHostedNotice('');
    const generation = lifetime.current;
    try {
      const result = await api<HostedState>('/hosted', { method: 'POST', body: JSON.stringify({ providers: hostedSelection ?? hosted.providers }) });
      if (generation !== lifetime.current) return;
      setHosted(result); setHostedSelection(null); setHostedNotice(copy('settings_hosted_saved_notice'));
    } catch (error) { if (generation === lifetime.current) setHostedNotice(errorText(error)); }
    finally { hostedMutation.current = false; if (generation === lifetime.current) { setHostedBusy(false); void refreshHosted(); } }
  };
  const answerHosted = async (approval: HostedApproval, result: Record<string, unknown>) => {
    const key = `${approval.agent_id}:${approval.approval_id}`;
    if (demo || !online || !hostedOnline || hostedMutation.current || switchingRef.current || hostedBlockedReplies.includes(key)) return;
    const currentApproval = hosted?.approvals.find(item => item.agent_id === approval.agent_id && item.approval_id === approval.approval_id && item.job_id === approval.job_id);
    if (!currentApproval) return;
    if (currentApproval.can_approve !== true && (result.decision === 'accept' || result.behavior === 'allow' || result.action === 'accept' || 'answers' in result)) { setHostedNotice(copy('approval_blocked_incomplete')); return; }
    const generation = lifetime.current, text = draftValue.current, reply = replyValue.current;
    const attempt = Object.freeze({ revision: inputRevision.current, text, reply });
    hostedMutation.current = true; ++hostedRead.current; setHostedBusy(true); setHostedNotice('');
    try {
      await api('/hosted/approval', { method: 'POST', body: JSON.stringify({ agent_id: approval.agent_id, approval_id: approval.approval_id, result }) });
      if (generation !== lifetime.current) return;
      setHostedBlockedReplies(current => [...current, key]);
      setHostedNotice(copy('approval_decision_sent_notice'));
      if (reply?.hosted?.agentId === approval.agent_id && reply.id === approval.approval_id && inputRevision.current === attempt.revision && replyValue.current === attempt.reply && draftValue.current === attempt.text) {
        draftValue.current = ''; setDraft(''); replyValue.current = null; setReplyTo(null); saveDraft('', null, null);
      }
    } catch (error) {
      if (generation === lifetime.current) { setHostedBlockedReplies(current => [...current, key]); setHostedNotice(copy('approval_decision_unknown_error', { error: errorText(error) })); }
    } finally { hostedMutation.current = false; if (generation === lifetime.current) { setHostedBusy(false); void refreshHosted(); } }
  };
  const chooseHostedReply = (approval: HostedApproval) => {
    if (approval.can_approve !== true) return;
    chooseReply({ id: approval.approval_id, label: approval.provider, hosted: { agentId: approval.agent_id, index: 0, answers: {} } });
    requestAnimationFrame(() => composer.current?.focus());
  };
  const setProfile = (provider: string, lane: string, field: string, value: string) => setTeam(current => current ? { ...current, providers: { ...current.providers, [provider]: { ...current.providers?.[provider], [lane]: { ...current.providers?.[provider]?.[lane], [field]: value || null } } } } : current);
  const waiting = state.participants.filter(participant => participant.status === 'awaiting_native_reply').length;
  const ready = state.participants.filter(participant => participant.readiness?.can_dispatch).length;
  const approvalWaiting = !!hosted?.approvals.length;
  const connectionText = demo ? copy('demo_mode_badge') : !online ? copy('room_status_offline') : paused ? copy('room_status_paused') : !busConnected ? copy('room_status_bus_waiting') : approvalWaiting ? copy('room_status_waiting_approval') : working ? copy('room_status_working_count', { count: working }) : waiting ? copy('room_status_waiting_reply_count', { count: waiting }) : ready ? copy('room_status_ready_count', { count: ready }) : state.participants.length ? copy('room_status_registered_none_ready', { count: state.participants.length }) : copy('room_status_empty_waiting');
  const dispatches = Object.entries(state.dispatches || {});
  const newestGoal = state.goals?.[0]?.id;
  const currentDispatch = newestGoal ? state.dispatches?.[newestGoal] : dispatches.at(-1)?.[1];
  const participantName = (actor: string) => state.participants.find(p => p.id === actor)?.name || (['user', 'human'].includes(actor) ? copy('conversation_actor_self') : copy('message_kind_message'));
  const currentMessages = newestGoal ? state.messages.filter(message => message.root_id === newestGoal) : demo ? state.messages : [];
  const focusedParticipant = state.participants.find(participant => participant.id === selectedSession);
  const visibleMessages = selectedSession ? state.messages.filter(message => message.actor === selectedSession || message.recipient === selectedSession) : state.messages;
  const recentSpeakers = [...new Set([...currentMessages].reverse().map(message => message.actor).filter(actor => seats.some(seat => seat.key === actor)))].slice(0, 2);
  const focusMessage = (id: string) => {
    setConversationOpen(true); setSelectedSession(null);
    if (matchMedia('(max-width: 1099px)').matches) { setInspectorOpen(false); setRailOpen(false); }
    requestAnimationFrame(() => {
      const target = document.getElementById(`conversation-${id}`);
      target?.scrollIntoView({ block: 'nearest', behavior: reducedMotion ? 'auto' : 'smooth' }); target?.focus({ preventScroll: true });
    });
  };
  const selectSession = (id: string | null) => { setSelectedSession(id); if (id) { setInspectorOpen(true); setInspectorTab('activity'); } if (matchMedia('(max-width: 1099px)').matches) setRailOpen(false); };
  const replyApproval = replyTo?.hosted ? hosted?.approvals.find(item => item.approval_id === replyTo.id && item.agent_id === replyTo.hosted?.agentId) : null;
  const replyQuestion = replyApproval ? (replyApproval.questions || replyApproval.details?.questions || [])[replyTo?.hosted?.index || 0] : null;
  const latestMessage = currentMessages.at(-1);
  const discovered = state.discovery?.filter(candidate => candidate.installed || candidate.running).length || 0;
  const placeholder = !online ? 'composer_placeholder_offline' : paused ? 'composer_placeholder_paused' : approvalWaiting && !replyTo ? 'composer_placeholder_waiting' : state.messages.length ? 'composer_placeholder_active' : 'composer_placeholder_idle';
  const openSettings = () => { setRailOpen(false); setInspectorOpen(false); setDrawer('settings'); };
  const eventLine = approvalWaiting ? copy('event_line_waiting_approval', { actor: hosted?.approvals[0]?.provider || '' }) : latestMessage ? (latestMessage.recipient ? copy('event_line_peer_summary', { actor: participantName(latestMessage.actor), recipient: participantName(latestMessage.recipient), summary: messageKind(latestMessage.kind) }) : `${participantName(latestMessage.actor)} · ${messageKind(latestMessage.kind)}`) : copy('event_line_idle');
  return <main className={`app ade-shell ${railOpen ? 'rail-open' : ''} ${inspectorOpen ? 'inspector-open' : ''} ${demo ? 'demo' : ''} ${conversationOpen ? 'conversation-open' : ''}`}>
    {railOpen && <><button className="rail-scrim" aria-label={copy('nav_workspace_rail_close')} onClick={closeRail} /><WorkspaceRail state={state} inventory={summary?.inventory} inventoryOnline={summaryOnline && online} workspaces={workspaces} selectedSession={selectedSession} native={!!window.avalokitaDesktop && !demo} busy={switching} notice={workspaceNotice} online={online} inert={inspectorFloating && inspectorOpen} onChoose={() => void openWorkspace()} onActivate={id => void openWorkspace(id)} onSelect={selectSession} onClose={closeRail} onSettings={openSettings} /></>}
    <div className="council-column" aria-busy={switching} inert={(narrow && railOpen) || (inspectorFloating && inspectorOpen)}>
      <header className="app-header">
        <button className="icon-button rail-toggle" aria-label={copy('nav_workspace_rail_toggle')} aria-expanded={railOpen} onClick={() => setRailOpen(!railOpen)}><Icon name="rail" /></button>
        <h1 className="brand-title"><span className="brand-wave" aria-hidden="true">∿</span>{copy('app_name')}</h1>
        <button className="workspace-chip" onClick={() => setRailOpen(true)}>{state.workspace.name || copy('workspace_connecting_label')}<span aria-hidden="true">⌄</span></button>
        <nav className="room-actions" aria-label={copy('settings_drawer_title')}>
          <button className="icon-button control-toggle" aria-label={copy(paused ? 'nav_resume_work' : 'nav_pause_work')} title={copy(paused ? 'nav_resume_work' : 'nav_pause_work')} disabled={!online || demo || controlBusy || switching} onClick={() => void control()}><Icon name={paused ? 'play' : 'pause'} /></button>
          <button className="icon-button history-toggle" aria-label={copy('nav_history_toggle')} title={copy('nav_history_toggle')} onClick={() => setDrawer('history')}><Icon name="history" /></button>
          <button className="icon-button inspector-toggle" aria-label={copy('nav_inspector_toggle')} aria-expanded={inspectorOpen} onClick={() => setInspectorOpen(!inspectorOpen)}><Icon name="inspect" /></button>
          <button className="icon-button settings-toggle" aria-label={copy('nav_settings_toggle')} title={copy('nav_settings_toggle')} onClick={openSettings}><Icon name="settings" /></button>
        </nav>
      </header>
      <div className="workspace-context"><div><p>{copy('workspace_context_breadcrumb', { name: state.workspace.name || copy('app_name') })}</p><h2>{state.goals?.[0]?.prompt || copy('workspace_goal_idle')}</h2></div><span className={`room-status ${working ? 'working' : ''} ${approvalWaiting ? 'waiting' : ''}`} role="status"><i className={`connection-dot ${online && busConnected ? 'online' : ''}`} />{connectionText}</span></div>
      <section className={`room count-${seats.length}`} aria-label={copy('scene_aria_label')}>
        <CouncilScene seats={seats} messages={state.messages} currentRoot={newestGoal} workspaceKey={workspaceKey} reducedMotion={reducedMotion} demo={demo} paused={paused || !online || !busConnected || approvalWaiting} interactionRevision={interactionRevision} onAnchors={setAnchors} />
        {!seats.length && <div className="room-empty-pond"><span className="empty-ripple" aria-hidden="true" /><h2>{copy('empty_room_title')}</h2><p>{copy('empty_room_body')}</p>{discovered > 0 && <button className="discovered-hint" onClick={() => setRailOpen(true)}>{copy('empty_room_discovered_hint', { count: discovered })}</button>}</div>}
        <div className={`seat-overlays count-${seats.length}`}>
          {seats.map((seat, index) => {
            const message = [...currentMessages].reverse().find(message => message.actor === seat.key);
            const body = readableMessage(message?.body || '');
            const anchor = anchors[seat.key];
            const showBubble = recentSpeakers[0] === seat.key || (recentSpeakers[1] === seat.key && !!anchor && !!anchors[recentSpeakers[0]] && Math.abs(anchor.x - anchors[recentSpeakers[0]].x) > 255);
            return <div key={seat.key} className={`seat-overlay seat-${index} provider-${seat.provider} ${recentSpeakers[0] === seat.key ? 'latest-speech' : ''}`} style={{ left: anchor?.x ?? `${20 + index * 30}%`, top: anchor?.y ?? '30%' }}>
              {body && showBubble && <button className={`speech-bubble ${message?.question_pending ? 'waiting' : ''}`} data-message-id={message?.id} data-root-id={message?.root_id} data-recipient={message?.recipient || undefined} onClick={() => message && focusMessage(message.id)} aria-label={copy('bubble_open_full_aria', { name: seat.label })}><span className="bubble-meta"><b>{messageKind(message?.kind)}</b>{message?.recipient && <span>→ {participantName(message.recipient)}</span>}</span><span className="bubble-text">{body}</span></button>}
            </div>;
          })}
        </div>
        <div className={`seat-nameplates count-${seats.length}`}>{seats.map(seat => <div className={`seat-nameplate provider-${seat.provider}`} key={seat.key} style={{ left: anchors[seat.key]?.x ?? '50%', top: anchors[seat.key]?.footY ?? 220 }}><button className={`seat-label ${selectedSession === seat.key ? 'selected' : ''}`} onClick={() => selectSession(seat.key)} aria-label={copy('seat_inspect_aria', { name: seat.label })}><strong>{seat.label}</strong><small><i className={online && !paused && isWorking(seat.participant) ? 'working' : ''} />{demo ? copy('message_kind_example') : !online ? copy('seat_status_unconfirmed') : paused ? copy('seat_status_paused') : statusLabel(seat.participant)}</small></button></div>)}</div>
        {state.participants.length > seats.length && <button className="seat-overflow text-button" onClick={() => setRailOpen(true)}>{copy('seat_overflow_more', { count: state.participants.length - seats.length })}</button>}
      </section>
      {conversationOpen && (state.messages.length > 0 || currentDispatch) && <section className="conversation" aria-label={copy('conversation_heading_all')}>
        <div className="conversation-heading"><h2>{focusedParticipant ? copy('conversation_heading_focused', { name: focusedParticipant.name }) : copy('conversation_heading_all')}<span className="recent-label">{copy('conversation_recent_badge')}</span></h2>{selectedSession ? <button className="text-button" onClick={() => setSelectedSession(null)}>{copy('nav_back_to_all_sessions')}</button> : <button className="icon-button" aria-label={copy('nav_drawer_close')} onClick={() => setConversationOpen(false)}><Icon name="close" /></button>}</div>
        <div className="conversation-scroll" ref={transcript} onScroll={event => { const area = event.currentTarget; followConversation.current = area.scrollHeight - area.scrollTop - area.clientHeight < 60; }} role="log" aria-live="polite" aria-relevant="additions text" aria-label={copy('conversation_heading_all')}>
          {!visibleMessages.length && <p className="filtered-empty">{copy('conversation_filtered_empty')}</p>}<ol>{visibleMessages.map(message => <li key={message.id} id={`conversation-${message.id}`} tabIndex={-1} className={['user', 'human'].includes(message.actor) ? 'human-message' : ''}>
            <div className="message-meta"><strong>{participantName(message.actor)}{message.recipient && <span className="message-recipient"> → {participantName(message.recipient)}</span>}</strong><span className="message-kind">{messageKind(message.kind)}</span><time>{messageTime(message.created_at)}</time></div><MessageBody body={message.body} human={['user', 'human'].includes(message.actor)} />
            {message.question_pending && message.question_id && <button className="reply-button" onClick={() => { chooseReply({ id: message.question_id!, label: participantName(message.actor) }); composer.current?.focus(); }}>{copy('conversation_reply_question_button')}</button>}
            {message.delivery && <small>{deliveryLabel(message.delivery)}</small>}
          </li>)}</ol>
        </div>
        {currentDispatch?.outcomes && <div className="recipient-progress">{currentDispatch.outcomes.map((outcome, index) => <span key={outcome.job_id || `${outcome.agent_id || 'session'}:${index}`}>{outcome.agent_id ? participantName(outcome.agent_id) : copy('roster_section_title')} · {deliveryLabel(outcome.status || outcome.state)}</span>)}</div>}
      </section>}
      <section className="conversation-entry" aria-label={copy('composer_label_sr')}>
        <HostedApprovals approvals={hosted?.approvals || []} busy={hostedBusy || !hostedOnline || !online || switching} blockedReplies={hostedBlockedReplies} notice={hostedNotice} onReply={chooseHostedReply} onDecision={(approval, result) => void answerHosted(approval, result)} />
        {paused && <div className="paused-banner"><span>{copy('composer_paused_hint')}</span><button className="text-button" disabled={!online || controlBusy || switching || demo} onClick={() => void control()}>{copy('control_resume_cta_button')}</button></div>}
        {replyTo && <div className="reply-banner"><span>{copy('reply_banner_active', { name: replyTo.label })}</span><button className="icon-button" onClick={() => chooseReply(null)} aria-label={copy('reply_banner_cancel')}><Icon name="close" /></button></div>}
        {replyTo?.hosted && <div className="hosted-question"><p>{replyQuestion ? copy('reply_question_step_prompt', { index: replyTo.hosted.index + 1, question: replyQuestion.question }) : replyApproval?.method === 'mcpServer/elicitation/request' ? copy('reply_question_elicitation_json_prompt') : copy('reply_question_missing_or_offline')}</p>{replyQuestion?.options?.map(option => <button className="reply-button" key={option.label} title={option.description} onClick={() => updateDraft(option.label)}>{option.label}</button>)}</div>}
        <button className={`public-event-line ${approvalWaiting ? 'waiting' : ''}`} aria-expanded={conversationOpen} onClick={() => setConversationOpen(!conversationOpen)}><span aria-hidden="true">•</span><span>{eventLine}</span><span aria-hidden="true">{conversationOpen ? '⌃' : '⌄'}</span></button>
        {replyTo && <div className="composer-target">{copy('composer_target_question_reply', { name: replyTo.label })}</div>}
        {selectedSession && <p className="composer-filter-note">{copy('composer_target_filter_note')}</p>}
        <div className="composer-wrap"><form onSubmit={event => { event.preventDefault(); void send(); }}><label className="sr-only" htmlFor="prompt">{copy('composer_label_sr')}</label><textarea id="prompt" ref={composer} rows={2} value={draft} disabled={switching} placeholder={copy(placeholder)} onFocus={() => setInteractionRevision(value => value + 1)} onChange={event => updateDraft(event.target.value)} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; if (!switchingRef.current) releaseExternalNavigation(); }} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); void send(); } }} aria-describedby="composer-hint ai-disclosure" /><button className="send-button" type="submit" aria-label={copy('composer_send_button')} disabled={!draft.trim() || sending || hostedBusy || paused || !online || switching}><Icon name="send" /></button></form></div>
        <div className="composer-footer"><span id="composer-hint" className="shortcut-hint">{copy('composer_shortcut_hint')}</span>{draft && !demo && <span className="draft-status">{copy(draftSaved === 'saved' ? 'draft_status_saved' : draftSaved === 'saving' ? 'draft_status_saving' : 'draft_status_local')}</span>}</div>
        <p className="notice" role="status">{switching ? copy('workspace_switching_busy') : sending ? copy('composer_sending_status') : notice || workspaceNotice || (!online ? copy('composer_offline_draft_kept') : demo ? copy('demo_mode_notice') : '')}</p>
        <p className="ai-disclosure" id="ai-disclosure">{copy('ai_disclosure')}</p>
      </section>
      <dialog className="drawer" ref={dialog} onCancel={() => setDrawer(null)} onClose={() => setDrawer(null)} aria-labelledby="drawer-title"><div className="drawer-header"><h2 id="drawer-title">{copy(drawer === 'history' ? 'history_drawer_title' : 'settings_drawer_title')}</h2><button className="icon-button" aria-label={copy('nav_drawer_close')} onClick={() => setDrawer(null)}><Icon name="close" /></button></div>
        {drawer === 'history' ? <div className="history"><p className="drawer-description">{copy('history_drawer_description')}</p>{!state.messages.length && <div className="empty-history"><span aria-hidden="true">◌</span><h3>{copy('history_empty_title')}</h3><p>{copy('history_empty_body')}</p></div>}<ol>{state.messages.map(message => <li key={message.id}><div className="message-meta"><strong>{participantName(message.actor)}{message.recipient && <span className="message-recipient"> → {participantName(message.recipient)}</span>}</strong><span className="message-kind">{messageKind(message.kind)}</span><time>{messageTime(message.created_at)}</time></div><MessageBody body={message.body} human={['user', 'human'].includes(message.actor)} />{message.question_pending && message.question_id && <button className="reply-button" onClick={() => { chooseReply({ id: message.question_id!, label: participantName(message.actor) }); setDrawer(null); requestAnimationFrame(() => composer.current?.focus()); }}>{copy('conversation_reply_question_button')}</button>}{message.delivery && <small>{deliveryLabel(message.delivery)}</small>}</li>)}</ol></div> : <div className="settings"><p className="drawer-description">{copy('settings_drawer_description')}</p>
          {demo ? <p className="quiet">{copy('demo_settings_readonly')}</p> : <>
            <HostedSettings hosted={hosted} selection={hostedSelection ?? hosted?.providers ?? []} busy={hostedBusy || !hostedOnline || switching} notice={hostedNotice || (!hostedOnline ? copy('settings_hosted_status_offline') : '')} onChange={setHostedSelection} onSave={() => void saveHosted()} />
            {team && <section><h3>{copy('settings_roles_title')}</h3>{Object.entries(team.roles || {}).map(([key, role]) => <div className="role-field" key={key}><label htmlFor={`role-${key}`}>{key}</label><input id={`role-${key}`} value={role.label || ''} onChange={e => setTeam({ ...team, roles: { ...team.roles, [key]: { ...role, label: e.target.value } } })} /><label htmlFor={`provider-${key}`}>{copy('settings_role_preferred_provider')}</label><input id={`provider-${key}`} value={role.preferred_provider || ''} onChange={e => setTeam({ ...team, roles: { ...team.roles, [key]: { ...role, preferred_provider: e.target.value } } })} /></div>)}<button className="save-button" onClick={() => void saveTeam()} disabled={teamBusy}>{copy(teamBusy ? 'settings_hosted_saving_button' : 'settings_team_save_button')}</button></section>}
            <p className="settings-notice" role="status">{teamNotice}</p>
          </>}
          <section className="help-bridge-card"><h3>{copy('help_entry_card_title')}</h3><p>{copy('help_entry_card_body')}</p><p className="quiet">{copy('help_entry_readonly_note')}</p><p className="pending-capability">{copy('help_entry_bridge_pending_notice')}</p></section>
          <details className="dev-diagnostics"><summary>{copy('dev_diagnostics_toggle_summary')}</summary><p>{copy('dev_diagnostics_description')}</p><h3>{copy('dev_diagnostics_workspace_path_label')}</h3><p className="workspace-path">{state.workspace.path}</p>{state.participants.map(p => <div className="participant-row" key={p.id}><strong>{p.name}</strong><code>{p.id}</code></div>)}
            {summary?.inventory && <pre>{JSON.stringify(summary.inventory, null, 2)}</pre>}
            {!demo && team && <section><h3>{copy('dev_diagnostics_manual_model_section')}</h3>{Object.entries(team.providers || {}).map(([provider, lanes]) => <fieldset key={provider}><legend>{provider}</legend>{Object.entries(lanes).filter(([, profile]) => typeof profile === 'object' && profile !== null).map(([lane, profile]) => <div className="profile-field" key={lane}><h4>{lane}</h4><label>display_model<input value={profile.display_model || ''} onChange={e => setProfile(provider, lane, 'display_model', e.target.value)} /></label><label>model_id<input value={profile.model_id || ''} onChange={e => setProfile(provider, lane, 'model_id', e.target.value)} /></label><label>effort<input value={profile.effort || ''} onChange={e => setProfile(provider, lane, 'effort', e.target.value)} /></label></div>)}</fieldset>)}<button className="save-button" onClick={() => void saveTeam()} disabled={teamBusy}>{copy('settings_team_save_button')}</button></section>}
          </details>
        </div>}
      </dialog>
    </div>
    {inspectorOpen && <><button className="inspector-scrim" aria-label={copy('nav_inspector_close')} onClick={closeInspector} /><WorkInspector state={state} selectedSession={selectedSession} selectedGoal={selectedGoal} tab={inspectorTab} online={online} onTab={setInspectorTab} onGoal={setSelectedGoal} onClose={closeInspector} onMessage={focusMessage} /></>}
  </main>;
}
