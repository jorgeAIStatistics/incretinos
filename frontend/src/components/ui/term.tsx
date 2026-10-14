"use client";

import * as Popover from "@radix-ui/react-popover";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { GLOSSARY, type GlossaryKey } from "@/lib/glossary";

/**
 * A technical term with its plain-English explanation one click away. The trigger is a real button (Enter, Space
 * or tap opens it); the explanation is a small dialog that closes on Escape, on its close button or on a click
 * elsewhere, and returns focus to the term. It never opens on hover alone, so it cannot cover what you are reading.
 *
 *   <Term k="estimand">estimand</Term>            wording from the sentence, definition from the glossary
 *   <Term k="confidence-interval" />              uses the glossary's own wording
 *
 * Definitions live in lib/glossary.ts so a term is explained the same way everywhere.
 */
export function Term({ k, children }: { k: GlossaryKey; children?: ReactNode }) {
  const entry: { term: string; definition: string; caution?: string } = GLOSSARY[k];
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          type="button"
          aria-label={`${typeof children === "string" ? children : entry.term}: what this means`}
          className="cursor-help rounded-chip text-left font-[inherit] text-inherit underline decoration-ink-3 decoration-dotted decoration-1 underline-offset-[3px] transition-colors duration-(--dur-fast) hover:decoration-accent hover:decoration-solid data-[state=open]:decoration-accent data-[state=open]:decoration-solid"
        >
          {children ?? entry.term.toLowerCase()}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content side="top" align="start" sideOffset={6} collisionPadding={12} className="popover-panel z-(--z-popover) w-[min(22rem,calc(100vw-1.5rem))] rounded-panel border border-hairline bg-surface p-3.5 text-data leading-relaxed text-ink-2 shadow-raised">
          <div className="flex items-start justify-between gap-3">
            <p className="font-semibold text-ink">{entry.term}</p>
            <Popover.Close aria-label="Close explanation" className="-mr-1.5 -mt-1 grid size-7 shrink-0 place-items-center rounded-chip text-ink-3 transition-colors duration-(--dur-fast) hover:bg-hover hover:text-ink">
              <X aria-hidden size={14} />
            </Popover.Close>
          </div>
          <p className="mt-1">{entry.definition}</p>
          {entry.caution ? <p className="mt-1.5 border-t border-hairline pt-1.5 text-ink">{entry.caution}</p> : null}
          <Popover.Arrow className="fill-(--surface)" />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
