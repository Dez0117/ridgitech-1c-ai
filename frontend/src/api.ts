export interface User {
  id: string;
  name: string;
  email: string;
}

export interface Conversation {
  id: string;
  title: string | null;
  updated_at: string;
  preview?: string | null;
}

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  created_at: string;
}

export interface Run {
  id: string;
  status: 'queued' | 'running' | 'completed' | 'needs_clarification' | 'failed';
  user_message_id: string;
  assistant_message_id: string | null;
  error: { message?: string } | null;
}

export const isActive = (run: Run) => run.status === 'queued' || run.status === 'running';

export class ApiError extends Error {
  constructor(readonly status: number, readonly detail: unknown) {
    super(typeof detail === 'string' && status < 500 ? detail : 'Не удалось выполнить запрос.');
  }

  get runId(): string | undefined {
    if (this.detail && typeof this.detail === 'object' && 'run_id' in this.detail) {
      if (typeof this.detail.run_id === 'string') return this.detail.run_id;
    }
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return 'Проверьте подключение и попробуйте ещё раз.';
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch('/api/v1' + path, {
    cache: 'no-store',
    ...options,
    headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, body?.detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

function post<T>(path: string, body: unknown) {
  return request<T>(path, { method: 'POST', body: JSON.stringify(body) });
}

export const api = {
  me: (signal: AbortSignal) => request<User>('/auth/me', { signal }),
  login: (body: { email: string; password: string }) => post<User>('/auth/login', body),
  register: (body: { name: string; email: string; password: string }) => post<User>('/auth/register', body),
  logout: () => request<void>('/auth/session', { method: 'DELETE' }),
  conversations: (signal?: AbortSignal) => request<Conversation[]>('/conversations', { signal }),
  createConversation: () => post<Conversation>('/conversations', {}),
  deleteConversation: (id: string) => request<void>('/conversations/' + id, { method: 'DELETE' }),
  messages: (id: string, signal?: AbortSignal, fresh = false) =>
    request<Message[]>('/conversations/' + id + '/messages' + (fresh ? '?fresh=true' : ''), { signal }),
  runs: (id: string, signal?: AbortSignal) =>
    request<Run[]>('/conversations/' + id + '/agent-runs', { signal }),
  run: (id: string, signal: AbortSignal) => request<Run>('/agent-runs/' + id, { signal }),
  send: (id: string, content: string) =>
    post<{ user_message: Message; run: Run }>('/conversations/' + id + '/messages', { content }),
};
