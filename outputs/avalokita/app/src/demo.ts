import type { RoomState } from './types';
// Deliberately isolated. Never imported into the live state unless ?demo=1 is explicit.
export const DEMO_STATE: RoomState = {
  workspace: { name: 'Avalokita 예시 작업실', path: '예시 / shared-workspace' },
  connection: { state: 'demo', message: '예시 화면 · 실제 에이전트 대화가 아닙니다' },
  participants: [
    { id: 'example-claude', provider: 'claude', name: 'Claude', role: 'Backend & Infra', status: 'working', attachment: 'demo', verified: false },
    { id: 'example-codex', provider: 'codex', name: 'Codex', role: 'Frontend & Design', status: 'working', attachment: 'demo', verified: false },
    { id: 'example-gemini', provider: 'gemini', name: 'Gemini', role: 'Ideation & Research', status: 'working', attachment: 'demo', verified: false },
  ],
  messages: [
    { id: 'example-1', actor: 'example-gemini', provider: 'gemini', body: '처음 만나는 순간부터, 함께 만든다는 느낌은 어떨까요?', created_at: 0, kind: 'example' },
    { id: 'example-2', actor: 'example-codex', provider: 'codex', body: '좋아요. 저는 원탁과 캐릭터의 움직임을 다듬을게요.', created_at: 0, kind: 'example' },
    { id: 'example-3', actor: 'example-claude', provider: 'claude', body: '대화가 이어지도록 연결과 작업 기록을 맡을게요.', created_at: 0, kind: 'example' },
  ], discovery: [],
};
