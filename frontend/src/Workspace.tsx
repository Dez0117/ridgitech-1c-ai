import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { History, LogOut, MessageSquare, PanelLeftClose, PanelLeftOpen, Plus, Search, Settings2, Trash2, X } from 'lucide-react';
import { api, ApiError, errorMessage } from './api';
import type { Conversation, User } from './api';
import { Brand } from './Brand';
import { ChatPanel } from './ChatPanel';
import { Modal } from './Modal';
import { Settings } from './Settings';
import type { Preferences } from './preferences';

interface WorkspaceProps {
  user: User;
  themeButton: ReactNode;
  onSignedOut: () => void;
  preferences: Preferences;
  onPreferencesChange: (patch: Partial<Preferences>) => void;
}

function initialConversation() {
  const value = new URLSearchParams(location.search).get('chat');
  return value && /^[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$/i.test(value) ? value.toLowerCase() : null;
}

const dateFormat = new Intl.DateTimeFormat('ru', { day: 'numeric', month: 'short' });

export function Workspace({
  user, themeButton, onSignedOut, preferences, onPreferencesChange,
}: WorkspaceProps) {
  const [selected, setSelected] = useState(initialConversation);
  const [chats, setChats] = useState<Conversation[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState('');
  const [logoutError, setLogoutError] = useState('');
  const [loggingOut, setLoggingOut] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Pick<Conversation, 'id' | 'title'> | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [query, setQuery] = useState('');
  const [initialDraft, setInitialDraft] = useState('');
  const [draftVersion, setDraftVersion] = useState(0);
  const historyRequest = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const drawerToggle = useRef<HTMLButtonElement>(null);
  const drawerClose = useRef<HTMLButtonElement>(null);

  const handleError = useCallback((error: unknown) => {
    if (!mounted.current) return '';
    if (error instanceof ApiError && error.status === 401) {
      onSignedOut();
      return '';
    }
    return errorMessage(error);
  }, [onSignedOut]);

  const refreshHistory = useCallback(() => {
    if (!mounted.current) return;
    historyRequest.current?.abort();
    const controller = new AbortController();
    historyRequest.current = controller;
    setHistoryError('');
    api.conversations(controller.signal).then(saved => {
      if (!controller.signal.aborted) setChats(saved);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setHistoryError(handleError(error));
    }).finally(() => {
      if (!controller.signal.aborted) setHistoryLoading(false);
    });
  }, [handleError]);

  useEffect(() => {
    mounted.current = true;
    refreshHistory();
    return () => {
      mounted.current = false;
      historyRequest.current?.abort();
    };
  }, [refreshHistory]);

  useEffect(() => {
    if (!drawer) return;
    const compactScreen = window.matchMedia('(max-width: 760px)');
    drawerClose.current?.focus();
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape' && !event.defaultPrevented && !document.querySelector('dialog[open]')) {
        setDrawer(false);
      }
    }
    function closeOnResize(event: MediaQueryListEvent) {
      if (!event.matches) setDrawer(false);
    }
    window.addEventListener('keydown', closeOnEscape);
    compactScreen.addEventListener('change', closeOnResize);
    return () => {
      window.removeEventListener('keydown', closeOnEscape);
      compactScreen.removeEventListener('change', closeOnResize);
      if (compactScreen.matches) drawerToggle.current?.focus();
    };
  }, [drawer]);

  function openChat(id: string | null, draft = '') {
    const url = new URL(location.href);
    if (id) url.searchParams.set('chat', id);
    else url.searchParams.delete('chat');
    history.replaceState(null, '', url);
    setSelected(id);
    setInitialDraft(draft);
    if (!id) setDraftVersion(version => version + 1);
    setDrawer(false);
  }

  async function signOut() {
    setLoggingOut(true);
    setLogoutError('');
    try {
      await api.logout();
      openChat(null);
      onSignedOut();
    } catch (error) {
      setLogoutError(handleError(error));
      setLoggingOut(false);
    }
  }

  function requestDelete(target: Pick<Conversation, 'id' | 'title'>) {
    setDeleteError('');
    setDeleteTarget(target);
  }

  async function deleteChat() {
    if (!deleteTarget || deleting) return;
    const { id } = deleteTarget;
    setDeleting(true);
    setDeleteError('');
    try {
      await api.deleteConversation(id);
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 404)) {
        setDeleteError(error instanceof ApiError && error.status === 409 && error.runId
          ? '1Сенсей ещё готовит ответ. Дождитесь его и попробуйте снова.'
          : handleError(error));
        return;
      }
    } finally {
      setDeleting(false);
    }
    setDeleteTarget(null);
    setChats(current => current.filter(chat => chat.id !== id));
    if (id === selected) openChat(null);
    refreshHistory();
  }

  const term = query.trim().toLocaleLowerCase('ru');
  const filteredChats = chats.filter(chat =>
    [chat.title, chat.preview].some(value => value?.toLocaleLowerCase('ru').includes(term)) || !term);
  const selectedChat = chats.find(chat => chat.id === selected);
  const collapsed = preferences.sidebarCollapsed;
  const sidebarAction = collapsed ? 'Показать левую панель' : 'Скрыть левую панель';

  return (
    <div className={'workspace' + (collapsed ? ' workspace-collapsed' : '')}>
      {drawer && <button className="sidebar-overlay" onClick={() => setDrawer(false)} aria-label="Закрыть историю" />}
      <aside id="chat-history" className={'sidebar' + (drawer ? ' sidebar-open' : '')} aria-label="История чатов">
        <div className="sidebar-top">
          <Brand />
          <button className="icon-button mobile-only" ref={drawerClose} aria-label="Закрыть историю"
            onClick={() => setDrawer(false)}>
            <X size={20} />
          </button>
        </div>
        <button className="primary new-chat" type="button" onClick={() => openChat(null)}>
          <Plus size={19} /> Новый чат
        </button>
        <label className="history-search">
          <Search size={17} />
          <input type="search" placeholder="Поиск по истории" aria-label="Поиск по истории" value={query}
            onChange={event => setQuery(event.target.value)} />
        </label>
        <div className="history-title"><History size={15} /> История чатов</div>
        <nav className="history-list" aria-label="Сохранённые чаты">
          {historyError && <div className="history-status" role="alert">{historyError}{' '}
            <button className="text-button" onClick={refreshHistory}>Повторить</button>
          </div>}
          {historyLoading && !historyError && <p className="history-status" role="status">Загрузка истории…</p>}
          {!historyLoading && !historyError && filteredChats.length === 0 &&
            <p className="history-status">{term ? 'Чаты не найдены.' : 'Здесь появятся ваши чаты.'}</p>}
          {filteredChats.map(chat => (
            <div className="history-entry" key={chat.id}>
              <button className="history-item" type="button"
                aria-current={selected === chat.id ? 'true' : undefined} onClick={() => openChat(chat.id)}>
                <MessageSquare size={16} />
                <span className="history-item-copy">
                  <span className="history-item-title">{chat.title || 'Новый чат'}</span>
                  {chat.preview && <span className="history-preview">{chat.preview}</span>}
                </span>
                <time dateTime={chat.updated_at}>{dateFormat.format(new Date(chat.updated_at))}</time>
              </button>
              <button className="icon-button delete-chat" type="button"
                aria-label={'Удалить чат «' + (chat.title || 'Новый чат') + '»'} title="Удалить чат"
                onClick={() => requestDelete(chat)}><Trash2 size={16} /></button>
            </div>
          ))}
        </nav>
        <div className="account-area">
          {logoutError && <p className="error-text" role="alert">{logoutError}</p>}
          <div className="account">
            <span className="avatar" aria-hidden="true">{Array.from(user.name)[0]?.toLocaleUpperCase('ru')}</span>
            <div className="account-copy"><strong>{user.name}</strong><span>{user.email}</span></div>
            <button className="icon-button" type="button" aria-label="Выйти" title="Выйти" onClick={signOut}
              disabled={loggingOut}><LogOut size={18} /></button>
          </div>
        </div>
      </aside>
      <main className="workspace-main" aria-label="Чат с 1Сенсеем" inert={drawer}>
        <header className="workspace-header">
          <div className="header-title">
            <button className="icon-button mobile-only" ref={drawerToggle} aria-label="Открыть историю" aria-expanded={drawer}
              aria-controls="chat-history" onClick={() => setDrawer(true)}><PanelLeftOpen size={20} /></button>
            <button className="icon-button desktop-only" type="button" aria-label={sidebarAction}
              title={sidebarAction} aria-expanded={!collapsed} aria-controls="chat-history"
              onClick={() => onPreferencesChange({ sidebarCollapsed: !collapsed })}>
              {collapsed ? <PanelLeftOpen size={20} /> : <PanelLeftClose size={20} />}
            </button>
            <span className="header-product">1Сенсей</span>
            <span className="header-divider" aria-hidden="true">/</span>
            <span className="header-chat">{selectedChat?.title || (selected ? 'Чат' : 'Новый чат')}</span>
          </div>
          <div className="header-actions">
            {selectedChat && <button className="icon-button delete-chat" type="button" aria-label="Удалить текущий чат"
              title="Удалить текущий чат" onClick={() => requestDelete(selectedChat)}><Trash2 size={18} /></button>}
            <button className="icon-button" type="button" aria-label="Настройки" title="Настройки"
              onClick={() => setSettingsOpen(true)}><Settings2 size={20} /></button>
            {themeButton}
          </div>
        </header>
        <ChatPanel key={selected ?? draftVersion} id={selected} initialDraft={initialDraft}
          openChat={openChat} refreshHistory={refreshHistory} handleError={handleError} preferences={preferences} />
      </main>
      {settingsOpen && <Settings preferences={preferences} onChange={onPreferencesChange}
        onClose={() => setSettingsOpen(false)} />}
      {deleteTarget && <Modal title="Удалить чат?" locked={deleting} onClose={() => setDeleteTarget(null)}>
        <p className="delete-description">«{deleteTarget.title || 'Новый чат'}» и все его сообщения будут удалены.</p>
        {deleteError && <p className="error-text" role="alert">{deleteError}</p>}
        <form className="modal-actions" method="dialog">
          <button className="secondary" type="submit" autoFocus disabled={deleting}>Отмена</button>
          <button className="danger-button" type="button" disabled={deleting} onClick={deleteChat}>
            {deleting ? 'Удаляем…' : 'Удалить чат'}
          </button>
        </form>
      </Modal>}
    </div>
  );
}
