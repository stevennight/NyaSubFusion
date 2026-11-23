import { useEffect, useState, type MouseEvent } from "react";

// 桌面版没有系统标题栏：顶栏自己负责拖动窗口、双击最大化 / 还原，右上角放最小化 · 最大化 · 关闭。
// 浏览器里打开（没有 __TAURI__）时什么都不做，界面照旧。
type TauriWindow = {
  startDragging: () => Promise<void>;
  toggleMaximize: () => Promise<void>;
  minimize: () => Promise<void>;
  close: () => Promise<void>;
  isMaximized: () => Promise<boolean>;
  onResized: (cb: () => void) => Promise<() => void>;
};
const appWindow = (): TauriWindow | null =>
  (window as unknown as { __TAURI__?: { window?: { getCurrentWindow?: () => TauriWindow } } }).__TAURI__?.window?.getCurrentWindow?.() ?? null;

export const isDesktop = () => appWindow() != null;
export const isMacDesktop = () =>
  isDesktop() && typeof navigator !== "undefined" && /Mac/i.test(navigator.platform || navigator.userAgent);

// 顶栏里这些东西照常点，不拿来拖窗口
const INTERACTIVE = "a, button, input, select, textarea, label, [role=button], .chip, .nodrag";

/** 顶栏的 onMouseDown：按住空白处拖动窗口，双击最大化 / 还原。 */
export function dragWindow(e: MouseEvent) {
  const w = appWindow();
  if (!w || e.button !== 0 || (e.target as HTMLElement).closest(INTERACTIVE)) return;
  e.preventDefault();
  (e.detail === 2 ? w.toggleMaximize() : w.startDragging()).catch(() => { /* 旧的桌面壳没有权限：不动 */ });
}

export function WindowControls() {
  const [w] = useState(appWindow);   // getCurrentWindow() 每次都给新对象，只取一次
  const [max, setMax] = useState(false);
  useEffect(() => {
    if (!w) return;
    let off: (() => void) | undefined, gone = false;
    const sync = () => { w.isMaximized().then((m) => { if (!gone) setMax(m); }).catch(() => {}); };
    sync();
    w.onResized(sync).then((f) => { if (gone) f(); else off = f; }).catch(() => {});
    return () => { gone = true; off?.(); };
  }, [w]);
  if (!w || isMacDesktop()) return null;
  const run = (p: Promise<void>) => { p.catch(() => {}); };
  return (
    <div className="wctl">
      <button title="最小化" onClick={() => run(w.minimize())}>
        <svg viewBox="0 0 10 10"><path d="M0 5.5h10" /></svg>
      </button>
      <button title={max ? "还原" : "最大化"} onClick={() => run(w.toggleMaximize())}>
        {max
          ? <svg viewBox="0 0 10 10"><path d="M0.5 2.5h7v7h-7z M2.5 2.5v-2h7v7h-2" /></svg>
          : <svg viewBox="0 0 10 10"><path d="M0.5 0.5h9v9h-9z" /></svg>}
      </button>
      <button className="x" title="关闭" onClick={() => run(w.close())}>
        <svg viewBox="0 0 10 10"><path d="M0.5 0.5l9 9M9.5 0.5l-9 9" /></svg>
      </button>
    </div>
  );
}
