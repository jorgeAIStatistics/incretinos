"use client";

import clsx from "clsx";
import { useEffect, useRef } from "react";
import { Modal } from "@/components/ui";

/** A key cap. Decorative inside a button whose name already says what it does. */
export function Kbd({ children, className, label }: { children: string; className?: string; /** Read out instead of hiding the cap. */ label?: boolean }) {
  return (
    <kbd aria-hidden={label ? undefined : true} className={clsx("mono inline-grid min-w-5 place-items-center rounded-[4px] border border-control bg-surface px-1 text-micro font-medium leading-4 text-ink-2", className)}>
      {children}
    </kbd>
  );
}

export const SHORTCUTS: { keys: string[]; what: string; where: string }[] = [
  { keys: ["j"], what: "Next record", where: "Queue and record" },
  { keys: ["k"], what: "Previous record", where: "Queue and record" },
  { keys: ["Enter", "o"], what: "Open the highlighted record", where: "Queue" },
  { keys: ["a"], what: "Approve…", where: "Record" },
  { keys: ["r"], what: "Reject…", where: "Record" },
  { keys: ["i"], what: "Request information…", where: "Record" },
  { keys: ["f"], what: "Flag wrong attribution…", where: "Record" },
  { keys: ["c"], what: "Propose a correction…", where: "Record" },
  { keys: ["/"], what: "Search the queue", where: "Queue" },
  { keys: ["Esc"], what: "Close a dialog; otherwise back to the queue", where: "Record" },
  { keys: ["?"], what: "Show these shortcuts", where: "Anywhere on this page" },
];

export function ShortcutHelp({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Modal open={open} onOpenChange={onOpenChange} title="Keyboard shortcuts" description="Shortcuts work when the focus is not in a text field. An action key opens its dialog; nothing is recorded without confirming there.">
      <table className="tbl tbl-compact">
        <caption className="sr-only">Keyboard shortcuts of the evidence review workbench</caption>
        <thead>
          <tr>
            <th scope="col">Keys</th>
            <th scope="col">Action</th>
            <th scope="col">Where</th>
          </tr>
        </thead>
        <tbody>
          {SHORTCUTS.map((s) => (
            <tr key={s.what}>
              <td className="whitespace-nowrap">
                {s.keys.map((k, i) => (
                  <span key={k}>
                    {i > 0 ? <span className="mx-1 text-ink-3">or</span> : null}
                    <Kbd label>{k}</Kbd>
                  </span>
                ))}
              </td>
              <th scope="row" className="!font-normal">{s.what}</th>
              <td className="text-ink-3">{s.where}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Modal>
  );
}

/** True when a key press belongs to a text field, a menu or an open dialog rather than to the page. */
function ownedElsewhere(e: KeyboardEvent): boolean {
  if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return true;
  const t = e.target instanceof HTMLElement ? e.target : null;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.closest('[role="listbox"],[role="menu"],[role="combobox"]'))) return true;
  // Enter on a focused link or button activates that control, not the page shortcut.
  if (e.key === "Enter" && t?.closest("a,button,summary,[role='button'],[role='tab']")) return true;
  // A dialog, popover or tooltip handles its own keys (Escape closes it first).
  return document.querySelector('[role="dialog"],[role="alertdialog"]') !== null;
}

/**
 * Page-level single-key shortcuts. `handlers` is read on every key press, so it may change on every render without
 * re-binding the listener.
 */
export function useShortcuts(handlers: Record<string, (() => void) | undefined>) {
  const ref = useRef(handlers);
  useEffect(() => {
    ref.current = handlers;
  });
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (ownedElsewhere(e)) return;
      const key = e.key === "Esc" ? "Escape" : e.key;
      const handler = ref.current[key];
      if (!handler) return;
      e.preventDefault();
      handler();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
