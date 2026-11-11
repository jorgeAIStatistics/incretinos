"use client";

import * as RadixTabs from "@radix-ui/react-tabs";
import clsx from "clsx";
import { type ReactNode, useRef } from "react";
import { useIndicator } from "@/lib/hooks";

/*
 * Tabs on the Radix primitive (arrow keys move between tabs, Home and End jump, the panel is labelled by its tab).
 * One underline slides to the selected tab in 160 ms; under reduced motion it jumps. Panels fade in.
 * Radix unmounts the panels that are not selected: state that must survive a tab switch belongs in the parent.
 */

export type TabItem = { value: string; label: string; /** Row count or similar, shown after the label. */ count?: ReactNode };

export function Tabs({ value, onValueChange, items, label, children, className }: { value: string; onValueChange: (value: string) => void; items: TabItem[]; /** Names the tab list. */ label: string; children: ReactNode; className?: string }) {
  const list = useRef<HTMLDivElement>(null);
  const box = useIndicator(list, '[role="tab"][data-state="active"]', value);
  return (
    <RadixTabs.Root value={value} onValueChange={onValueChange} className={className}>
      {/* The scroll box clips whatever overflows it, so it keeps 4px of room on every side for the 2px focus ring and its 2px offset. */}
      <div className="-mx-1 -mt-1 mb-3 overflow-x-auto px-1 pb-1 pt-1 [scrollbar-width:none]" data-testid="tab-scroll">
        <RadixTabs.List ref={list} aria-label={label} className="relative flex w-max min-w-full gap-5 border-b border-hairline">
          {items.map((item) => (
            <RadixTabs.Trigger
              key={item.value}
              value={item.value}
              className="shrink-0 whitespace-nowrap rounded-chip pb-2.5 pt-1 text-body font-medium text-ink-3 transition-colors duration-(--dur-fast) ease-standard hover:text-ink data-[state=active]:text-ink"
            >
              {item.label}
              {item.count !== undefined && item.count !== null ? <span className="num ml-1.5 text-data font-normal text-ink-3">{item.count}</span> : null}
            </RadixTabs.Trigger>
          ))}
          <span
            aria-hidden
            className={clsx("pointer-events-none absolute bottom-0 left-0 h-0.5 w-px origin-left bg-accent transition-transform duration-(--dur-base) ease-standard", !box && "opacity-0")}
            style={box ? { transform: `translateX(${box.x}px) scaleX(${box.width})` } : undefined}
          />
        </RadixTabs.List>
      </div>
      {children}
    </RadixTabs.Root>
  );
}

export function TabPanel({ value, children, className }: { value: string; children: ReactNode; className?: string }) {
  return (
    <RadixTabs.Content value={value} className={clsx("fade-in", className)}>
      {children}
    </RadixTabs.Content>
  );
}
