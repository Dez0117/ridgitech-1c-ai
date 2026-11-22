import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { BarChart3, CalendarDays, CircleAlert, ListOrdered, Send, Wallet } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, ApiError, isActive } from './api';
import type { Message, Run } from './api';
import { BrandMark } from './Brand';
import type { Preferences } from './preferences';

const prompts = [
  { title: 'Продажи за месяц', text: 'Покажи продажи за январь 2025 года', icon: BarChart3 },
  { title: 'Поступления денег', text: 'Покажи поступления по направлениям за январь 2025 года', icon: Wallet },
  { title: 'Сравнение периодов', text: 'Сравни продажи за январь 2024 и январь 2025 года', icon: CalendarDays },
  { title: 'Топ контрагентов', text: 'Покажи топ-5 контрагентов по продажам за 2025 год', icon: ListOrdered },
];

interface ChatPanelProps {
  id: string | null;
  initialDraft: string;
  openChat: (id: string, draft?: string) => void;
  refreshHistory: () => void;
  handleError: (error: unknown) => string;
  preferences: Pick<Preferences, 'showSuggestions' | 'autoScroll' | 'enterToSend'>;
}

const maxLength = 10_000;

export function ChatPanel({ id, initialDraft, openChat, refreshHistory, handleError, preferences }: ChatPanelProps) {
  const [draft, setDraft] = useState(initialDraft);
  const [messages, setMessages] = useState<Message[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(Boolean(id));
  const [sending, setSending] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [sendError, setSendError] = useState('');
  const [pollError, setPollError] = useState('');
  const [loadVersion, setLoadVersion] = useState(0);
  const mounted = useRef(true);
  const input = useRef<HTMLTextAreaElement>(null);
  const history = useRef<HTMLDivElement>(null);
  const activeRunId = runs.find(isActive)?.id;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    api.runs(id, controller.signal)
      .then(async jobs => {
        const saved = await api.messages(id, controller.signal, true);
        if (controller.signal.aborted) return;
        setMessages(saved);
        setRuns(jobs);
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) setLoadError(handleError(error));
      }).finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [id, loadVersion, handleError]);

  useEffect(() => {
    if (!id || !activeRunId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let failures = 0;
    async function check(conversationId: string, runId: string) {
      try {
        const run = await api.run(runId, controller.signal);
        if (controller.signal.aborted) return;
        if (!isActive(run)) {
          const saved = await api.messages(conversationId, controller.signal, true);
          if (controller.signal.aborted) return;
          setMessages(saved);
          setRuns(current => current.map(item => item.id === run.id ? run : item));
          setPollError('');
          refreshHistory();
          return;
        }
        failures = 0;
        setPollError('');
      } catch (error) {
        if (controller.signal.aborted) return;
        const detail = handleError(error);
        failures += 1;
        if (detail && failures >= 2) setPollError('Связь с сервером прервалась. Продолжаем проверять ответ.');
      }
      timer = setTimeout(() => void check(conversationId, runId), failures ? 4500 : 1400);
    }
    timer = setTimeout(() => void check(id, activeRunId), 800);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id, activeRunId, handleError, refreshHistory]);

  useEffect(() => {
    const panel = history.current;
    if (panel && preferences.autoScroll) panel.scrollTop = panel.scrollHeight;
  }, [messages.length, activeRunId, preferences.autoScroll]);

  function reload() {
    setLoadError('');
    setLoading(true);
    setLoadVersion(version => version + 1);
  }

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || sending || loading || loadError || activeRunId) return;
    setSending(true);
    setSendError('');
    let conversationId = id;
    try {
      if (!conversationId) conversationId = (await api.createConversation()).id;
      const response = await api.send(conversationId, content);
      if (mounted.current) {
        setDraft('');
        if (!id) openChat(conversationId);
        else {
          setMessages(current => [...current, response.user_message]);
          setRuns(current => [...current, response.run]);
        }
      }
      refreshHistory();
    } catch (error) {
      const detail = handleError(error);
      const saved = error instanceof ApiError && error.status === 503 && Boolean(error.runId);
      const inProgress = error instanceof ApiError && error.status === 409 && Boolean(error.runId);
      if (mounted.current) {
        if (!id && conversationId && detail) openChat(conversationId, saved ? '' : content);
        else if (saved) {
          setDraft('');
          reload();
        } else if (detail) {
          setSendError(inProgress ? 'В этом чате уже обрабатывается запрос.' : detail);
          if (inProgress) reload();
        }
      }
      if (conversationId) refreshHistory();
    } finally {
      if (mounted.current) setSending(false);
    }
  }

  const empty = !loading && !loadError && messages.length === 0;
  const blocked = loading || sending || Boolean(loadError || activeRunId);
  const failedRuns = new Map(runs.filter(run => run.status === 'failed' && !run.assistant_message_id)
    .map(run => [run.user_message_id, run]));

  return (
    <>
      <div className="chat-scroll" ref={history}>
        {loading && messages.length === 0 && <p className="chat-status" role="status">Загружаем сообщения…</p>}
        {loadError && <div className="chat-status" role="alert">
          <p>{loadError}</p><button className="primary" onClick={reload}>Повторить</button>
        </div>}
        {empty && <section className="welcome" aria-labelledby="welcome-title">
          <BrandMark className="welcome-mark" />
          <h1 id="welcome-title">1Сенсей</h1>
          {preferences.showSuggestions && <p>Задайте вопрос или выберите пример запроса.</p>}
          {preferences.showSuggestions && <div className="prompt-grid">
            {prompts.map(({ title, text, icon: Icon }) => (
              <button className="prompt-card" type="button" key={title} disabled={sending} onClick={() => {
                setDraft(text);
                input.current?.focus();
              }}>
                <span className="prompt-icon"><Icon size={18} /></span>
                <span><strong>{title}</strong><small>{text}</small></span>
              </button>
            ))}
          </div>}
        </section>}
        {messages.length > 0 && <div className="message-list">
          {messages.map(message => {
            const sentByUser = message.role === 'user';
            const failure = failedRuns.get(message.id);
            return <article className={'message ' + (sentByUser ? 'message-user' : 'message-assistant')} key={message.id}>
              {!sentByUser && <BrandMark className="message-mark" />}
              <div className="message-group">
                <div className="message-label">{sentByUser ? 'Вы' : '1Сенсей'}</div>
                <div className="message-body">
                  {sentByUser ? <p className="user-text">{message.content}</p>
                    : <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml
                        components={{ a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
                          img: ({ alt }) => alt || null }}>{message.content}</ReactMarkdown>}
                </div>
                {failure && <p className="message-failure">
                  <CircleAlert size={15} /> {failure.error?.message ?? 'Запрос завершился ошибкой.'}
                </p>}
              </div>
            </article>;
          })}
          {activeRunId && <p className="pending" role="status"><span /> 1Сенсей готовит ответ…</p>}
        </div>}
      </div>
      <div className="composer-area">
        {pollError && <p className="error-text" role="status">{pollError}</p>}
        {sendError && <p className="error-text" role="alert">{sendError}</p>}
        <form className="composer" onSubmit={send}>
          <textarea ref={input} aria-label="Сообщение для 1Сенсея" value={draft} maxLength={maxLength} rows={2}
            placeholder="Напишите вопрос…" readOnly={sending}
            onChange={event => setDraft(event.target.value)}
            onKeyDown={event => {
              if (preferences.enterToSend && event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }} />
          <button className="primary send" type="submit" disabled={blocked || !draft.trim()}
            aria-label="Отправить сообщение" title="Отправить сообщение"><Send size={19} /></button>
        </form>
        <div className="composer-hint">
          {preferences.enterToSend && <span>Enter — отправить · Shift + Enter — новая строка</span>}
          {draft.length >= 9000 && <span>{draft.length} / {maxLength}</span>}
        </div>
      </div>
    </>
  );
}
