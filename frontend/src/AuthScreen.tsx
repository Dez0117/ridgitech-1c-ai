import { useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { api, ApiError, errorMessage } from './api';
import type { User } from './api';
import { Brand } from './Brand';

interface AuthProps {
  onAuthenticated: (user: User) => void;
  themeButton: ReactNode;
}

export function AuthScreen({ onAuthenticated, themeButton }: AuthProps) {
  const [register, setRegister] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const form = new FormData(event.currentTarget);
    const credentials = { email: String(form.get('email')), password: String(form.get('password')) };
    setError('');
    setBusy(true);
    try {
      const user = register
        ? await api.register({ ...credentials, name: String(form.get('name')) })
        : await api.login(credentials);
      onAuthenticated(user);
    } catch (cause) {
      setError(cause instanceof ApiError && cause.status === 422
        ? 'Проверьте заполнение полей.'
        : errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-layout">
      <section className="auth-main" aria-labelledby="auth-title">
        <div className="auth-theme">{themeButton}</div>
        <div className="auth-form">
          <Brand />
          <h1 id="auth-title">{register ? 'Создать аккаунт' : 'Вход в аккаунт'}</h1>
          <form onSubmit={submit}>
            {register && <label>Имя
              <input name="name" autoComplete="name" maxLength={80} required placeholder="Как к вам обращаться" />
            </label>}
            <label>Электронная почта
              <input name="email" type="email" autoComplete="username" maxLength={254} required
                placeholder="name@example.com" />
            </label>
            <label>Пароль
              <span className="password-field">
                <input key={String(register)} name="password" type={showPassword ? 'text' : 'password'}
                  autoComplete={register ? 'new-password' : 'current-password'} minLength={register ? 12 : 1}
                  maxLength={128} required placeholder={register ? 'Не менее 12 символов' : 'Введите пароль'} />
                <button className="icon-button" type="button"
                  aria-label={showPassword ? 'Скрыть пароль' : 'Показать пароль'}
                  onClick={() => setShowPassword(!showPassword)}>
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </span>
            </label>
            {error && <p className="error-text" role="alert">{error}</p>}
            <button className="primary auth-submit" type="submit" disabled={busy}>
              {busy ? 'Подождите…' : register ? 'Зарегистрироваться' : 'Войти'}
            </button>
          </form>
          <div className="auth-switch">
            {register ? 'Уже есть аккаунт?' : 'Нет аккаунта?'}
            <button className="text-button" type="button" disabled={busy} onClick={() => {
              setRegister(!register);
              setShowPassword(false);
              setError('');
            }}>{register ? 'Войти' : 'Зарегистрироваться'}</button>
          </div>
        </div>
      </section>
    </main>
  );
}
