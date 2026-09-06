import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';

/**
 * 界面偏好(仅本地体验设置,绝不存业务数据 — 业务记录的权威来源是后端 SQLite)。
 * localStorage 只保存:字号、时间显示偏好。
 */
export type FontSize = 'normal' | 'large';
export type TimezoneDisplay = 'local' | 'asia-shanghai';

interface Preferences {
  fontSize: FontSize;
  timezone: TimezoneDisplay;
}

interface PreferencesContextValue extends Preferences {
  setFontSize: (size: FontSize) => void;
  setTimezone: (tz: TimezoneDisplay) => void;
}

const KEY = 'mcp.preferences.v1';
const FALLBACK: Preferences = { fontSize: 'normal', timezone: 'local' };

function load(): Preferences {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return FALLBACK;
    const parsed = JSON.parse(raw) as Partial<Preferences>;
    return {
      fontSize: parsed.fontSize === 'large' ? 'large' : 'normal',
      timezone: parsed.timezone === 'asia-shanghai' ? 'asia-shanghai' : 'local',
    };
  } catch {
    return FALLBACK;
  }
}

const Ctx = createContext<PreferencesContextValue | null>(null);

export function PreferencesProvider({ children }: { children: React.ReactNode }): React.ReactElement {
  const [prefs, setPrefs] = useState<Preferences>(load);

  useEffect(() => {
    document.documentElement.dataset.fontsize = prefs.fontSize;
    try {
      window.localStorage.setItem(KEY, JSON.stringify(prefs));
    } catch {
      // 隐私模式等场景下保存失败不影响使用
    }
  }, [prefs]);

  const value = useMemo<PreferencesContextValue>(
    () => ({
      ...prefs,
      setFontSize: (fontSize) => setPrefs((p) => ({ ...p, fontSize })),
      setTimezone: (timezone) => setPrefs((p) => ({ ...p, timezone })),
    }),
    [prefs],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function usePreferences(): PreferencesContextValue {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('usePreferences must be used within PreferencesProvider');
  return ctx;
}

/** ISO 8601 → 按用户偏好时区显示;时间字符串缺时区时不假装它是本地时间。 */
export function formatTime(iso: string | null | undefined, prefs: Preferences): string {
  if (!iso) return '未记录';
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const d = new Date(hasZone ? iso : `${iso}+00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  const opts: Intl.DateTimeFormatOptions = {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
    timeZone: prefs.timezone === 'asia-shanghai' ? 'Asia/Shanghai' : undefined,
  };
  return new Intl.DateTimeFormat('zh-CN', opts).format(d);
}
