"use client";

import clsx from "clsx";
import { CheckCircle2, Info, ShieldAlert, X } from "lucide-react";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

/*
 * Transient confirmations ("Decision recorded"). The region is a polite live region, so a message is read out
 * without moving focus. Use a toast only for the result of an action the user just took; anything they must
 * read or act on belongs inline, next to what it concerns. Errors stay until dismissed.
 */

export type ToastTone = "ok" | "info" | "critical";
type Toast = { id: number; title: string; description?: string; tone: ToastTone };
type ToastApi = { toast: (t: { title: string; description?: string; tone?: ToastTone }) => void };

const ToastContext = createContext<ToastApi | null>(null);

const LOOK: Record<ToastTone, { Icon: typeof Info; cls: string }> = {
  ok: { Icon: CheckCircle2, cls: "text-ok" },
  info: { Icon: Info, cls: "text-accent" },
  critical: { Icon: ShieldAlert, cls: "text-critical" },
};

const LIFETIME_MS = 6000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(1);
  const timers = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    window.clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    setToasts((list) => list.filter((t) => t.id !== id));
  }, []);

  const arm = useCallback((id: number) => {
    window.clearTimeout(timers.current.get(id));
    timers.current.set(id, window.setTimeout(() => dismiss(id), LIFETIME_MS));
  }, [dismiss]);

  const toast = useCallback<ToastApi["toast"]>(
    ({ title, description, tone = "info" }) => {
      const id = nextId.current++;
      setToasts((list) => [...list.slice(-2), { id, title, description, tone }]);
      if (tone !== "critical") arm(id);
    },
    [arm],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach((t) => window.clearTimeout(t));
  }, []);

  const api = useMemo(() => ({ toast }), [toast]);
  return (
    <ToastContext.Provider value={api}>
      {children}
      <div role="region" aria-label="Notifications" className="pointer-events-none fixed inset-x-0 bottom-0 z-(--z-toast) flex flex-col items-end gap-2 p-4 pb-[calc(1rem+env(safe-area-inset-bottom))]">
        <div aria-live="polite" aria-atomic="false" className="flex w-full max-w-sm flex-col gap-2">
          {toasts.map((t) => {
            const { Icon, cls } = LOOK[t.tone];
            return (
              <div
                key={t.id}
                role={t.tone === "critical" ? "alert" : "status"}
                onMouseEnter={() => window.clearTimeout(timers.current.get(t.id))}
                onMouseLeave={() => t.tone !== "critical" && arm(t.id)}
                onFocus={() => window.clearTimeout(timers.current.get(t.id))}
                onBlur={() => t.tone !== "critical" && arm(t.id)}
                className="toast-item pointer-events-auto flex items-start gap-2.5 rounded-panel border border-hairline bg-surface px-3.5 py-3 text-data shadow-raised"
              >
                <Icon aria-hidden size={16} className={clsx("mt-0.5 shrink-0", cls)} />
                <div className="min-w-0 flex-1">
                  <p className="font-semibold text-ink">{t.title}</p>
                  {t.description ? <p className="break-words text-ink-2">{t.description}</p> : null}
                </div>
                <button type="button" aria-label="Dismiss notification" onClick={() => dismiss(t.id)} className="-mr-1 -mt-0.5 grid size-7 shrink-0 place-items-center rounded-chip text-ink-3 transition-colors duration-(--dur-fast) hover:bg-hover hover:text-ink">
                  <X aria-hidden size={14} />
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </ToastContext.Provider>
  );
}

/** `toast({ title, description?, tone? })`. Outside a ToastProvider it does nothing rather than throw. */
export function useToast(): ToastApi {
  return useContext(ToastContext) ?? { toast: () => undefined };
}
