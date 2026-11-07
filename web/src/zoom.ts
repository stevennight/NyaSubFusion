// 界面缩放：桌面版用 WebView 自己的缩放（和浏览器 Ctrl + / Ctrl - 一样，鼠标位置、时间轴拖动都跟着对），记在本机。
// 浏览器里打开的直接用浏览器自己的 Ctrl + / Ctrl -，这里不管。
const KEY = "nyasub.zoom";
export const ZOOMS = [0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5];

type Webview = { setZoom: (z: number) => Promise<void> };
const webview = (): Webview | null =>
  (window as unknown as { __TAURI__?: { webview?: { getCurrentWebview?: () => Webview } } }).__TAURI__?.webview?.getCurrentWebview?.() ?? null;

export const canZoom = () => webview() != null;

export function getZoom(): number {
  try { return Number(localStorage.getItem(KEY)) || 1; } catch { return 1; }
}

export function setZoom(z: number) {
  const v = Math.min(ZOOMS[ZOOMS.length - 1], Math.max(ZOOMS[0], Math.round(z * 100) / 100));
  try { localStorage.setItem(KEY, String(v)); } catch { /* 存不了就只在这次有效 */ }
  webview()?.setZoom(v).catch(() => { /* 没有权限（旧的桌面壳）：不缩放 */ });
  window.dispatchEvent(new Event("nyasub-zoom"));
}

/** 启动时套用记下的缩放，并接上 Ctrl + / Ctrl - / Ctrl 0。 */
export function initZoom() {
  if (!canZoom()) return;
  if (getZoom() !== 1) setZoom(getZoom());
  window.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
    const z = getZoom(), i = ZOOMS.findIndex((x) => x >= z - 0.001);
    if (e.key === "=" || e.key === "+") setZoom(ZOOMS[Math.min(ZOOMS.length - 1, (ZOOMS[i] > z + 0.001 ? i : i + 1))]);
    else if (e.key === "-" || e.key === "_") setZoom(ZOOMS[Math.max(0, i - 1)]);
    else if (e.key === "0") setZoom(1);
    else return;
    e.preventDefault();
  });
}
