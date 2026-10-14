"use client";

import { usePathname, useSearchParams } from "next/navigation";
import { type RefObject, useCallback, useEffect, useLayoutEffect, useState, useSyncExternalStore } from "react";

/**
 * Filters, tabs and selections live in the query string, so a view can be linked to, reloaded and restored.
 * Updates use the History API, which Next.js keeps in step with `useSearchParams`: the address changes without a
 * round trip to the server and without adding a history entry for every keystroke.
 * Requires a <Suspense> boundary above the component (Next.js `useSearchParams`).
 */
export function useQueryParams(): [get: (key: string, fallback?: string) => string, set: (patch: Record<string, string | null | undefined>) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const get = useCallback((key: string, fallback = "") => params.get(key) ?? fallback, [params]);
  const set = useCallback(
    (patch: Record<string, string | null | undefined>) => {
      const next = new URLSearchParams(window.location.search);
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === undefined || value === "") next.delete(key);
        else next.set(key, value);
      }
      const query = next.toString();
      if (query === window.location.search.replace(/^\?/, "")) return;
      window.history.replaceState(null, "", query ? `${pathname}?${query}` : pathname);
    },
    [pathname],
  );
  return [get, set];
}

/** Whether the media query matches, or null before the browser has been asked (server render and hydration). */
export function useMediaQuery(query: string): boolean | null {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", onChange);
      return () => list.removeEventListener("change", onChange);
    },
    [query],
  );
  return useSyncExternalStore<boolean | null>(subscribe, () => window.matchMedia(query).matches, () => null);
}

const useIsomorphicLayoutEffect = typeof window === "undefined" ? useEffect : useLayoutEffect;

export type IndicatorBox = { x: number; y: number; width: number; height: number } | null;

/**
 * Position of the element matching `selector` inside `container`, for a sliding "current item" indicator.
 * It is measured after layout (never during render) and again whenever the container resizes or `dependency` changes.
 */
export function useIndicator(container: RefObject<HTMLElement | null>, selector: string, dependency: unknown): IndicatorBox {
  const [box, setBox] = useState<IndicatorBox>(null);
  useIsomorphicLayoutEffect(() => {
    const root = container.current;
    if (!root) return;
    const measure = () => {
      const target = root.querySelector<HTMLElement>(selector);
      if (!target) return setBox(null);
      const next = { x: target.offsetLeft, y: target.offsetTop, width: target.offsetWidth, height: target.offsetHeight };
      setBox((prev) => (prev && prev.x === next.x && prev.y === next.y && prev.width === next.width && prev.height === next.height ? prev : next));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    return () => observer.disconnect();
  }, [container, selector, dependency]);
  return box;
}
