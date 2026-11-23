import { useEffect } from 'react';

import { canDismissModal } from './modalDismiss';

export function ModalShell({
  title,
  description,
  ariaLabel,
  actionPending,
  onClose,
  children,
  footer,
}: {
  title: string;
  description: string;
  ariaLabel: string;
  actionPending: boolean;
  onClose: () => void;
  children: React.ReactNode;
  footer: React.ReactNode;
}) {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (canDismissModal(actionPending)) onClose();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose, actionPending]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && canDismissModal(actionPending)) onClose();
      }}
    >
      <div className="w-full max-w-lg max-h-full flex flex-col bg-white rounded-2xl border border-slate-200 shadow-xl">
        <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-slate-200/60">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-900 leading-tight">{title}</h2>
            <p className="text-xs text-slate-500 mt-0.5">{description}</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={!canDismissModal(actionPending)}
            aria-label="Close"
            className="shrink-0 inline-flex items-center justify-center h-7 w-7 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors disabled:opacity-40"
          >
            <span aria-hidden="true" className="text-base leading-none">×</span>
          </button>
        </div>

        <div className="px-5 py-4 space-y-4 min-h-0 overflow-y-auto">{children}</div>

        <div className="flex items-center justify-end gap-2 px-5 py-4 border-t border-slate-200/60">
          {footer}
        </div>
      </div>
    </div>
  );
}
