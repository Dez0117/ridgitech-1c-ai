export const fonts = {
  system: 'Системный',
  sans: 'Arial',
  serif: 'Georgia',
  mono: 'Моноширинный',
} as const;

export const textSizes = [90, 100, 110, 120, 130];

export interface Preferences {
  theme: 'light' | 'dark';
  font: keyof typeof fonts;
  textSize: number;
  sidebarCollapsed: boolean;
  showSuggestions: boolean;
  autoScroll: boolean;
  enterToSend: boolean;
}

const storageKey = 'ridgitech-preferences';

export function defaultPreferences(): Preferences {
  return {
    theme: window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
    font: 'system',
    textSize: 100,
    sidebarCollapsed: false,
    showSuggestions: true,
    autoScroll: true,
    enterToSend: true,
  };
}

export function loadPreferences(): Preferences {
  const defaults = defaultPreferences();
  try {
    const saved: Partial<Preferences> | null = JSON.parse(localStorage.getItem(storageKey) ?? 'null');
    const theme = saved?.theme ?? localStorage.getItem('ridgitech-theme');
    const font = saved?.font;
    const textSize = saved?.textSize;
    return {
      theme: theme === 'dark' || theme === 'light' ? theme : defaults.theme,
      font: font && Object.hasOwn(fonts, font) ? font : defaults.font,
      textSize: typeof textSize === 'number' && textSizes.includes(textSize)
        ? textSize : defaults.textSize,
      sidebarCollapsed: saved?.sidebarCollapsed === true,
      showSuggestions: saved?.showSuggestions !== false,
      autoScroll: saved?.autoScroll !== false,
      enterToSend: saved?.enterToSend !== false,
    };
  } catch {
    return defaults;
  }
}

export function applyPreferences(preferences: Preferences) {
  const root = document.documentElement;
  root.dataset.theme = preferences.theme;
  root.dataset.font = preferences.font;
  root.style.fontSize = preferences.textSize + '%';
  try {
    localStorage.setItem(storageKey, JSON.stringify(preferences));
  } catch {
    // Preferences still apply for the current session.
  }
}
