"use client";

import clsx from "clsx";
import { Inbox, type LucideIcon, RotateCw, ShieldAlert } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";
import type { ApiState } from "@/lib/api";
import { Button } from "./button";

/*
 * The three states that are not "here is your data". They must never be confused with one another:
 *   loading  a skeleton shaped like the content, announced as "Loading…"
 *   error    says what failed and what to do next, with a retry
 *   empty    the request succeeded and there is nothing: says so in domain terms, and how to change that
 * A pending or failed request must never fall through to an empty state. Render through <Async>.
 */

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={clsx("skeleton", className)} />;
}

export function Loading({ rows = 4, label = "Loading…", className }: { rows?: number; label?: string; className?: string }) {
  return (
    <div role="status" aria-label={label} className={clsx("space-y-2", className)}>
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-9" style={{ width: `${100 - i * 7}%` }} />
      ))}
      <span className="sr-only">{label}</span>
    </div>
  );
}

/** Skeleton with the outline of a table, so the layout does not jump when rows arrive. */
export function TableSkeleton({ rows = 8, columns = 5, label = "Loading rows…" }: { rows?: number; columns?: number; label?: string }) {
  return (
    <div role="status" aria-label={label} className="overflow-hidden rounded-panel border border-hairline bg-surface">
      <div className="flex gap-4 border-b border-rule bg-sunken px-3 py-2.5">
        {Array.from({ length: columns }, (_, c) => (
          <div key={c} className="skeleton h-3 flex-1" />
        ))}
      </div>
      {Array.from({ length: rows }, (_row, r) => (
        <div key={r} className="flex gap-4 border-b border-hairline px-3 py-3 last:border-b-0">
          {Array.from({ length: columns }, (_cell, c) => (
            <div key={c} className="skeleton h-3.5 flex-1" style={{ maxWidth: c === 0 ? "none" : `${70 + ((r + c) % 3) * 10}%` }} />
          ))}
        </div>
      ))}
      <span className="sr-only">{label}</span>
    </div>
  );
}

export function EmptyState({ title, children, action, Icon = Inbox, className }: { title: string; children?: ReactNode; action?: ReactNode; Icon?: LucideIcon; className?: string }) {
  return (
    <div className={clsx("flex flex-col items-start gap-1 rounded-panel border border-dashed border-rule bg-surface px-4 py-5 text-data", className)}>
      <Icon aria-hidden size={18} className="mb-1 text-ink-3" />
      <p className="font-semibold text-ink">{title}</p>
      {children ? <div className="max-w-[68ch] text-ink-2">{children}</div> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function ErrorState({ title = "This could not be loaded", message, onRetry, className }: { title?: string; message: string; onRetry?: () => void; className?: string }) {
  return (
    <div role="alert" className={clsx("flex items-start gap-2.5 rounded-control border border-critical-border bg-critical-soft px-3.5 py-3 text-data", className)}>
      <ShieldAlert aria-hidden size={16} className="mt-0.5 shrink-0 text-critical" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-ink">{title}</p>
        <p className="break-words text-ink-2">{message}</p>
        {onRetry ? (
          <Button variant="secondary" size="sm" onClick={onRetry} Icon={RotateCw} className="mt-2">
            Try again
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/** Earlier, message-only form of ErrorState. */
export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex items-start gap-2 rounded-control border border-critical-border bg-critical-soft px-3 py-2 text-data text-critical">
      <ShieldAlert aria-hidden size={15} className="mt-0.5 shrink-0" />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      {onRetry ? (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

/**
 * Renders a request state. Loading, error and empty are three different outputs; `children` runs only for a
 * successful, non-empty answer. While a newer answer is being fetched the previous content stays, dimmed and
 * marked busy, so a filter change gives feedback without a flash of skeleton.
 */
export function Async<T>({
  state,
  children,
  loading,
  isEmpty,
  empty,
  errorTitle,
  className,
}: {
  state: ApiState<T>;
  children: (data: T) => ReactNode;
  loading?: ReactNode;
  /** When it returns true for a successful answer, `empty` is rendered instead of `children`. */
  isEmpty?: (data: T) => boolean;
  empty?: ReactNode;
  errorTitle?: string;
  className?: string;
}) {
  if (state.status === "idle") return null;
  if (state.status === "error") return <ErrorState title={errorTitle} message={state.error ?? "Unknown error"} onRetry={state.reload} className={className} />;
  if (state.status === "loading" || state.data === null) return <>{loading ?? <Loading className={className} />}</>;
  const data = state.data;
  return (
    <div aria-busy={state.refreshing || undefined} data-refreshing={state.refreshing ? "true" : "false"} className={clsx("fade-in", className)}>
      {isEmpty?.(data) ? (empty ?? <EmptyState title="Nothing to show" />) : children(data)}
    </div>
  );
}

/**
 * Catches a rendering error below it and shows a recoverable message instead of a blank page. One wraps the
 * page content in the shell; put another around any widget whose failure should not take the page with it.
 * Give it a `key` (the route, the selected record) to clear a caught error when that context changes.
 */
export class ErrorBoundary extends Component<{ children: ReactNode; title?: string }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Render error caught by ErrorBoundary:", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <ErrorState
        title={this.props.title ?? "This part of the page failed to display"}
        message={`${this.state.error.message || "An unexpected error occurred."} The rest of the application is unaffected. Try again, or reload the page if the problem persists.`}
        onRetry={() => this.setState({ error: null })}
      />
    );
  }
}
