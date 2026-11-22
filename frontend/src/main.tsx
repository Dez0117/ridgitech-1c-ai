import { useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Moon, Sun } from 'lucide-react';
import { api, ApiError, errorMessage } from './api';
import type { User } from './api';
import { AuthScreen } from './AuthScreen';
import { BrandMark } from './Brand';
import { Workspace } from './Workspace';
import { applyPreferences, loadPreferences } from './preferences';
import type { Preferences } from './preferences';
import './styles.css';

function App() {
  const [preferences, setPreferences] = useState(loadPreferences);
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [sessionError, setSessionError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const signOut = useCallback(() => setUser(null), []);
  const updatePreferences = useCallback((patch: Partial<Preferences>) => {
    setPreferences(current => ({ ...current, ...patch }));
  }, []);

  useEffect(() => {
    applyPreferences(preferences);
  }, [preferences]);

  useEffect(() => {
    const controller = new AbortController();
    api.me(controller.signal).then(setUser).catch((error: unknown) => {
      if (!controller.signal.aborted && !(error instanceof ApiError && error.status === 401)) {
        setSessionError(errorMessage(error));
      }
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [attempt]);

  const dark = preferences.theme === 'dark';
  const themeLabel = dark ? 'Включить светлую тему' : 'Включить тёмную тему';
  const themeButton = (
    <button className="icon-button" type="button" aria-label={themeLabel} title={themeLabel}
      onClick={() => updatePreferences({ theme: dark ? 'light' : 'dark' })}>
      {dark ? <Sun size={19} /> : <Moon size={19} />}
    </button>
  );

  if (loading || sessionError) return (
    <main className="app-status">
      <BrandMark />
      <p role="status">{sessionError || 'Проверяем сессию…'}</p>
      {sessionError && <button className="primary" onClick={() => {
        setSessionError('');
        setLoading(true);
        setAttempt(attempt + 1);
      }}>Повторить</button>}
    </main>
  );

  return user
    ? <Workspace key={user.id} user={user} themeButton={themeButton} onSignedOut={signOut}
        preferences={preferences} onPreferencesChange={updatePreferences} />
    : <AuthScreen themeButton={themeButton} onAuthenticated={setUser} />;
}

createRoot(document.getElementById('root')!).render(<App />);
