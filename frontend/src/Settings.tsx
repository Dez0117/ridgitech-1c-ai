import { ChevronDown } from 'lucide-react';
import { Modal } from './Modal';
import { defaultPreferences, fonts, textSizes } from './preferences';
import type { Preferences } from './preferences';

interface SettingsProps {
  preferences: Preferences;
  onChange: (patch: Partial<Preferences>) => void;
  onClose: () => void;
}

export function Settings({ preferences, onChange, onClose }: SettingsProps) {
  return (
    <Modal title="Настройки" onClose={onClose}>
      <fieldset className="settings-section">
        <legend>Внешний вид</legend>
        <label className="setting-row">Тема
          <span className="select-wrap">
            <select value={preferences.theme}
              onChange={event => onChange({ theme: event.target.value === 'dark' ? 'dark' : 'light' })}>
              <option value="light">Светлая</option>
              <option value="dark">Тёмная</option>
            </select>
            <ChevronDown size={16} aria-hidden="true" />
          </span>
        </label>
        <label className="setting-row">Шрифт
          <span className="select-wrap">
            <select value={preferences.font} onChange={event => onChange({ font: event.target.value as Preferences['font'] })}>
              {Object.entries(fonts).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
            <ChevronDown size={16} aria-hidden="true" />
          </span>
        </label>
        <label className="setting-row">Размер текста
          <span className="select-wrap">
            <select value={preferences.textSize} onChange={event => onChange({ textSize: Number(event.target.value) })}>
              {textSizes.map(size => <option key={size} value={size}>{size}%</option>)}
            </select>
            <ChevronDown size={16} aria-hidden="true" />
          </span>
        </label>
      </fieldset>
      <fieldset className="settings-section">
        <legend>Чат с 1Сенсеем</legend>
        <label className="setting-row">Показывать примеры запросов
          <input type="checkbox" checked={preferences.showSuggestions}
            onChange={event => onChange({ showSuggestions: event.target.checked })} />
        </label>
        <label className="setting-row">Автопрокрутка к новым сообщениям
          <input type="checkbox" checked={preferences.autoScroll}
            onChange={event => onChange({ autoScroll: event.target.checked })} />
        </label>
        <label className="setting-row">Отправлять сообщение по Enter
          <input type="checkbox" checked={preferences.enterToSend}
            onChange={event => onChange({ enterToSend: event.target.checked })} />
        </label>
      </fieldset>
      <button className="text-button reset-settings" type="button" onClick={() => onChange(defaultPreferences())}>
        Сбросить настройки
      </button>
    </Modal>
  );
}
