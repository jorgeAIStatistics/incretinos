"use client";

import * as Tooltip from "@radix-ui/react-tooltip";
import clsx from "clsx";
import { Atom, GitCompareArrows, LayoutDashboard, Library, Map as MapIcon, MessagesSquare, Microscope, Ruler, Scale, ShieldAlert, Target } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useApi } from "@/lib/api";

const NAV = [
  { href: "/", label: "Command Center", Icon: LayoutDashboard },
  { href: "/molecules", label: "Molecule Explorer", Icon: Atom },
  { href: "/trials", label: "Clinical Trial Atlas", Icon: MapIcon },
  { href: "/compare", label: "Comparison Laboratory", Icon: GitCompareArrows },
  { href: "/estimands", label: "Estimand Inspector", Icon: Microscope },
  { href: "/benefit-risk", label: "Benefit–Risk Workbench", Icon: Scale },
  { href: "/safety", label: "Safety Intelligence", Icon: ShieldAlert },
  { href: "/gaps", label: "Evidence Gap Tracker", Icon: Target },
  { href: "/design", label: "Protocol Design", Icon: Ruler },
  { href: "/assistant", label: "Research Assistant", Icon: MessagesSquare },
  { href: "/sources", label: "Sources & Audit", Icon: Library },
];

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { data: meta } = useApi("/meta");
  const isActive = (href: string) => (href === "/" ? pathname === "/" : pathname.startsWith(href));
  return (
    <Tooltip.Provider>
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2">
        Skip to content
      </a>
      <div className="lg:grid lg:min-h-screen lg:grid-cols-[236px_minmax(0,1fr)]">
        <aside className="border-b border-hairline bg-surface lg:sticky lg:top-0 lg:h-screen lg:border-b-0 lg:border-r">
          <div className="flex h-full flex-col">
            <Link href="/" className="flex items-center gap-2.5 px-4 py-4 text-ink hover:no-underline">
              <span aria-hidden className="grid h-8 w-8 place-items-center rounded-md bg-accent text-[13px] font-bold tracking-tight text-white">iO</span>
              <span className="leading-tight">
                <span className="block text-[15px] font-semibold">IncretinOS</span>
                <span className="block text-[11px] text-ink-3">Clinical evidence intelligence</span>
              </span>
            </Link>
            <nav aria-label="Primary" className="flex gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:overflow-visible lg:pb-0">
              {NAV.map(({ href, label, Icon }) => (
                <Link
                  key={href}
                  href={href}
                  aria-current={isActive(href) ? "page" : undefined}
                  className={clsx(
                    "flex shrink-0 items-center gap-2.5 rounded-md px-2.5 py-[7px] text-[13.5px] transition-colors hover:no-underline",
                    isActive(href) ? "bg-accent-soft font-semibold text-accent" : "text-ink-2 hover:bg-sunken hover:text-ink",
                  )}
                >
                  <Icon aria-hidden size={16} strokeWidth={isActive(href) ? 2.2 : 1.8} />
                  {label}
                </Link>
              ))}
            </nav>
            <div className="mt-auto hidden border-t border-hairline px-4 py-3 text-[11.5px] leading-snug text-ink-3 lg:block">
              <p className="font-medium text-ink-2">{meta ? meta.mode : "Connecting…"}</p>
              {meta ? (
                <p className="mt-0.5">
                  {meta.counts.studies} trials · {meta.counts.sources} sources
                  <br />
                  Assistant model: {meta.llm.enabled ? meta.llm.provider : "off (deterministic)"}
                </p>
              ) : null}
              <p className="mt-2">Public data only. Research use — not medical advice.</p>
            </div>
          </div>
        </aside>
        <main id="main" className="min-w-0 px-4 py-6 sm:px-7 lg:px-10 lg:py-8">
          <div className="mx-auto max-w-[1240px] fade-in">{children}</div>
        </main>
      </div>
    </Tooltip.Provider>
  );
}
