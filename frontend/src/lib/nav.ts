import type { LucideIcon } from "lucide-react";
import { Atom, ClipboardCheck, Gauge, GitCompareArrows, LayoutDashboard, Library, Map as MapIcon, MessagesSquare, Microscope, Ruler, Scale, ShieldAlert, Split, Target } from "lucide-react";

export type NavItem = { href: string; label: string; Icon: LucideIcon };
export type NavGroup = { id: string; label: string | null; items: NavItem[] };

/**
 * Primary navigation. Labels and routes of the eleven original entries are unchanged. The Review group holds
 * three pages that arrive later in this release; they exist as placeholders so the navigation is already final.
 */
export const NAV_GROUPS: NavGroup[] = [
  { id: "home", label: null, items: [{ href: "/", label: "Command Center", Icon: LayoutDashboard }] },
  {
    id: "evidence",
    label: "Evidence",
    items: [
      { href: "/molecules", label: "Molecule Explorer", Icon: Atom },
      { href: "/trials", label: "Clinical Trial Atlas", Icon: MapIcon },
      { href: "/compare", label: "Comparison Laboratory", Icon: GitCompareArrows },
      { href: "/estimands", label: "Estimand Inspector", Icon: Microscope },
      { href: "/benefit-risk", label: "Benefit–Risk Workbench", Icon: Scale },
      { href: "/safety", label: "Safety Intelligence", Icon: ShieldAlert },
    ],
  },
  {
    id: "planning",
    label: "Planning",
    items: [
      { href: "/gaps", label: "Evidence Gap Tracker", Icon: Target },
      { href: "/design", label: "Protocol Design", Icon: Ruler },
      { href: "/assistant", label: "Research Assistant", Icon: MessagesSquare },
    ],
  },
  {
    id: "review",
    label: "Review and provenance",
    items: [
      { href: "/review", label: "Evidence Review", Icon: ClipboardCheck },
      { href: "/quality", label: "Evidence Quality", Icon: Gauge },
      { href: "/conflicts", label: "Conflicts", Icon: Split },
      { href: "/sources", label: "Sources & Audit", Icon: Library },
    ],
  },
];

export const NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

export const isActivePath = (href: string, pathname: string): boolean => (href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`));
