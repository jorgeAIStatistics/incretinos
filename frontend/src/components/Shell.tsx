"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import clsx from "clsx";
import { Menu } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useId, useRef, useState } from "react";
import { Drawer, ErrorBoundary, IconButton, ToastProvider } from "@/components/ui";
import { useApi } from "@/lib/api";
import { useIndicator } from "@/lib/hooks";
import { isActivePath, NAV_GROUPS } from "@/lib/nav";
import type { Meta } from "@/lib/types";

const RESEARCH_NOTICE = "Public data only. Research use by qualified researchers. Not medical advice.";

function Brand({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <Link href="/" onClick={onNavigate} translate="no" className="flex min-w-0 items-center gap-2.5 rounded-control text-ink hover:no-underline">
      <span aria-hidden className="grid size-8 shrink-0 place-items-center rounded-control bg-accent text-data font-bold tracking-tight text-white">
        iO
      </span>
      <span className="min-w-0 leading-tight">
        <span className="block text-body font-semibold">IncretinOS</span>
        <span className="block truncate text-micro text-ink-3">Clinical evidence intelligence</span>
      </span>
    </Link>
  );
}

/**
 * The primary navigation list, used by the desktop sidebar and by the drawer on smaller screens.
 * A single bar slides to the current entry (160 ms; it jumps under reduced motion). The current entry is also
 * marked with aria-current, a tinted background and heavier text, so the bar is never the only cue.
 */
function PrimaryNav({ pathname, onNavigate }: { pathname: string; onNavigate?: () => void }) {
  const list = useRef<HTMLElement>(null);
  const box = useIndicator(list, 'a[aria-current="page"]', pathname);
  const id = useId();
  return (
    <nav ref={list} aria-label="Primary" className="relative px-2 pb-3">
      <span
        aria-hidden
        className={clsx("pointer-events-none absolute left-0 top-0 h-px w-[3px] origin-top rounded-r-full bg-accent transition-transform duration-(--dur-base) ease-standard", !box && "opacity-0")}
        style={box ? { transform: `translateY(${box.y + 6}px) scaleY(${Math.max(1, box.height - 12)})` } : undefined}
      />
      {NAV_GROUPS.map((group) => (
        <div key={group.id} role="group" aria-labelledby={group.label ? `${id}-${group.id}` : undefined} className={group.label ? "mt-4" : undefined}>
          {group.label ? (
            <p id={`${id}-${group.id}`} className="px-2.5 pb-1 text-micro font-semibold text-ink-3">
              {group.label}
            </p>
          ) : null}
          <ul className="flex flex-col gap-0.5">
            {group.items.map(({ href, label, Icon }) => {
              const active = isActivePath(href, pathname);
              return (
                <li key={href}>
                  <Link
                    href={href}
                    onClick={onNavigate}
                    aria-current={active ? "page" : undefined}
                    className={clsx(
                      "flex min-h-(--control-h) items-center gap-2.5 rounded-control px-2.5 text-data transition-colors duration-(--dur-fast) ease-standard hover:no-underline",
                      active ? "bg-accent-soft font-semibold text-accent" : "text-ink-2 hover:bg-hover hover:text-ink active:bg-sunken",
                    )}
                  >
                    <Icon aria-hidden size={16} strokeWidth={active ? 2.2 : 1.8} className="shrink-0" />
                    <span className="min-w-0 truncate">{label}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

/** Service status. Loading, failure and success are three different outputs: it never sits on "Connecting…" after a failure. */
function ServiceStatus() {
  const meta = useApi<Meta>("/meta");
  return (
    <div className="border-t border-hairline px-4 py-3 text-caption leading-snug text-ink-3">
      {meta.status === "error" ? (
        <p role="status">
          <span className="font-semibold text-critical">Evidence service not reachable.</span>{" "}
          <button type="button" onClick={meta.reload} className="rounded-chip font-medium text-accent underline underline-offset-2">
            Try again
          </button>
        </p>
      ) : meta.data ? (
        <>
          <p className="font-medium text-ink-2">{meta.data.mode}</p>
          <p className="num mt-0.5">
            {meta.data.counts.studies} trials, {meta.data.counts.sources} sources
            <br />
            Assistant model: {meta.data.llm.enabled ? meta.data.llm.provider : "off (deterministic)"}
          </p>
        </>
      ) : (
        <p role="status">Connecting to the evidence service…</p>
      )}
      <p className="mt-2">{RESEARCH_NOTICE}</p>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  // The drawer remembers the path it was opened on, so following a link closes it without an effect.
  const [menuPath, setMenuPath] = useState<string | null>(null);
  const menuOpen = menuPath !== null && menuPath === pathname;
  return (
    <Tooltip.Provider>
      <ToastProvider>
        <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-(--z-toast) focus:rounded-control focus:bg-surface focus:px-3 focus:py-2 focus:shadow-raised">
          Skip to content
        </a>

        {/* Below 1024 px: a top bar with a menu button; the navigation opens as a drawer. */}
        <header data-print="hide" className="sticky top-0 z-(--z-nav) flex h-(--topbar-h) items-center gap-2 border-b border-hairline bg-surface px-3 pt-[env(safe-area-inset-top)] lg:hidden">
          <IconButton label="Open navigation" Icon={Menu} onClick={() => setMenuPath(pathname)} aria-expanded={menuOpen} aria-haspopup="dialog" />
          <Brand />
        </header>
        <Drawer open={menuOpen} onOpenChange={(open) => setMenuPath(open ? pathname : null)} title="Navigation" side="left" width="18rem" hideTitle>
          <div className="flex min-h-full flex-col">
            <div className="flex h-(--topbar-h) shrink-0 items-center px-4 pr-14">
              <Brand onNavigate={() => setMenuPath(null)} />
            </div>
            <div className="flex-1 pt-1">
              <PrimaryNav pathname={pathname} onNavigate={() => setMenuPath(null)} />
            </div>
            <ServiceStatus />
          </div>
        </Drawer>

        <div className="lg:grid lg:grid-cols-[var(--sidebar-w)_minmax(0,1fr)]">
          {/* From 1024 px: a fixed sidebar. Its list scrolls on short screens; brand and status stay in view. */}
          <aside data-print="hide" className="sticky top-0 hidden h-dvh flex-col border-r border-hairline bg-surface lg:flex">
            <div className="flex h-16 shrink-0 items-center px-4">
              <Brand />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
              <PrimaryNav pathname={pathname} />
            </div>
            <ServiceStatus />
          </aside>

          <div className="flex min-h-[calc(100dvh-var(--topbar-h))] min-w-0 flex-col lg:min-h-dvh">
            {/* tabIndex -1 lets the skip link move focus here; main is never a tab stop, so it shows no ring. */}
            <main id="main" tabIndex={-1} className="min-w-0 flex-1 px-4 py-5 outline-none sm:px-7 sm:py-6 lg:px-10 lg:py-8">
              <div key={pathname} className="fade-in mx-auto max-w-[1240px]">
                <ErrorBoundary>{children}</ErrorBoundary>
              </div>
            </main>
            <footer className="border-t border-hairline px-4 py-3 text-caption text-ink-3 sm:px-7 lg:px-10">
              <p className="mx-auto max-w-[1240px]">{RESEARCH_NOTICE} Not a treatment recommendation and not a regulatory submission.</p>
            </footer>
          </div>
        </div>
      </ToastProvider>
    </Tooltip.Provider>
  );
}
