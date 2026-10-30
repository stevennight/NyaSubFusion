import type { ReactNode } from "react";
import { createPortal } from "react-dom";

/** 对话框：点外面关掉 */
export function Modal({ onClose, children, width = 560 }: { onClose: () => void; children: ReactNode; width?: number }) {
  return createPortal(
    <div className="modal" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal-body" style={{ width, maxWidth: "94vw" }}>{children}</div>
    </div>, document.body);
}
