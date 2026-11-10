"use client";

import { Eye, KeyRound, LogOut } from "lucide-react";
import { createContext, type FormEvent, type ReactNode, useCallback, useContext, useId, useMemo, useRef, useState } from "react";
import { Button, TextField } from "@/components/ui";
import type { ReviewerIdentityMode } from "@/lib/review";

/*
 * The reviewer's credential lives in React state of this tab only. It is never written to localStorage,
 * sessionStorage, a cookie, the address, the console or a DOM attribute: the token box is an uncontrolled
 * password input that is emptied as soon as the token has been taken into memory, so its text never becomes a
 * `value` attribute. Reloading the page or pressing "Forget token" discards it.
 */

type Session = {
  /** Whether the deployment accepts decisions at all (`review_enabled` from /api/meta). Null until known. */
  enabled: boolean | null;
  /**
   * How the service establishes the reviewer name (`reviewer_identity`). With PERSONAL_TOKENS the name is bound to
   * the token on the server: no name is typed or sent, and the name recorded is the one the service returns. Null
   * until known, in which case a name is asked for (the service refuses a name that is not the token's own).
   */
  identityMode: ReviewerIdentityMode | null;
  /** True when the service derives the reviewer name from the token (personal reviewer tokens). */
  serverNamesReviewer: boolean;
  token: string | null;
  reviewer: string;
  setReviewer: (name: string) => void;
  setToken: (token: string | null) => void;
  /** True when decisions can be sent: enabled, a token in memory and, unless the service names the reviewer, a reviewer name. */
  canDecide: boolean;
};

const ReviewerContext = createContext<Session | null>(null);

export function ReviewerSessionProvider({ enabled, identityMode = null, children }: { enabled: boolean | null; identityMode?: ReviewerIdentityMode | null; children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(null);
  const [reviewer, setReviewer] = useState("");
  const setToken = useCallback((next: string | null) => setTokenState(next && next.length ? next : null), []);
  const serverNamesReviewer = identityMode === "PERSONAL_TOKENS";
  const value = useMemo<Session>(
    () => ({ enabled, identityMode, serverNamesReviewer, token, reviewer, setReviewer, setToken, canDecide: enabled === true && !!token && (serverNamesReviewer || reviewer.trim().length >= 2) }),
    [enabled, identityMode, serverNamesReviewer, token, reviewer, setToken],
  );
  return <ReviewerContext.Provider value={value}>{children}</ReviewerContext.Provider>;
}

export function useReviewer(): Session {
  const session = useContext(ReviewerContext);
  if (!session) throw new Error("useReviewer outside ReviewerSessionProvider");
  return session;
}

/** One line stating the mode, for the page header: read-only public mode, or recording as a named reviewer. */
export function ModeLine() {
  const { enabled, token, reviewer, serverNamesReviewer } = useReviewer();
  if (enabled === null) return <p role="status" className="text-data text-ink-3">Checking whether this deployment accepts review decisions…</p>;
  const readOnly = !enabled || !token;
  return (
    <p data-testid="review-mode" className="inline-flex items-center gap-1.5 rounded-chip border border-control px-2 py-1 text-data text-ink-2">
      {readOnly ? <Eye aria-hidden size={14} /> : <KeyRound aria-hidden size={14} />}
      {readOnly ? (
        <span>
          <span className="font-semibold text-ink">Read-only public mode</span>
          {enabled ? " — enter the reviewer token to record decisions" : " — this deployment accepts no decisions"}
        </span>
      ) : (
        <span>
          <span className="font-semibold text-ink">Reviewer mode</span> — token held in memory for this tab
          {serverNamesReviewer ? ", recording under the name bound to your token" : reviewer.trim() ? `, recording as ${reviewer.trim()}` : ""}
        </span>
      )}
    </p>
  );
}

/** Token entry and the reviewer's name. Shown above the queue and inside the record view's action panel. */
export function ReviewerAccess() {
  const { enabled, token, reviewer, setReviewer, setToken, serverNamesReviewer } = useReviewer();
  const input = useRef<HTMLInputElement>(null);
  const tokenId = useId();
  const [tokenError, setTokenError] = useState<string | null>(null);

  if (enabled === null) return null;
  if (!enabled) {
    return (
      <p className="max-w-[80ch] text-data text-ink-2" data-testid="read-only-deployment">
        This deployment has no reviewer token configured, so it is read-only. You can inspect every record, its source, its checks and its history; decisions cannot be recorded here.
      </p>
    );
  }

  const take = (e: FormEvent) => {
    e.preventDefault();
    const value = input.current?.value ?? "";
    if (input.current) input.current.value = "";
    if (!value.trim()) {
      setTokenError("Enter the reviewer token.");
      return;
    }
    setTokenError(null);
    setToken(value);
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-end gap-3">
        {serverNamesReviewer ? null : <TextField label="Your name" name="reviewer" autoComplete="name" value={reviewer} onChange={setReviewer} className="w-full sm:w-56" />}
        {token ? (
          <div className="flex flex-wrap items-center gap-2">
            <p className="inline-flex items-center gap-1.5 text-data text-ink-2">
              <KeyRound aria-hidden size={14} /> Token held in memory
            </p>
            <Button variant="secondary" size="sm" Icon={LogOut} onClick={() => setToken(null)}>
              Forget token
            </Button>
          </div>
        ) : (
          <form onSubmit={take} className="flex flex-wrap items-end gap-2" aria-label="Enter the reviewer token">
            <div className="flex min-w-0 flex-col gap-1 text-caption font-medium text-ink-2">
              <label htmlFor={tokenId}>Reviewer token</label>
              <input
                ref={input}
                id={tokenId}
                type="password"
                name="reviewer-token"
                autoComplete="off"
                spellCheck={false}
                aria-invalid={tokenError ? true : undefined}
                aria-describedby={tokenError ? `${tokenId}-error` : `${tokenId}-hint`}
                className="h-(--control-h) w-full min-w-0 rounded-control border border-control bg-surface px-2.5 text-body font-normal text-ink hover:border-ink-2 sm:w-56"
              />
            </div>
            <Button type="submit" variant="secondary" Icon={KeyRound}>
              Use token
            </Button>
          </form>
        )}
      </div>
      {tokenError ? (
        <p id={`${tokenId}-error`} role="alert" className="text-caption text-critical">
          {tokenError}
        </p>
      ) : !token ? (
        <p id={`${tokenId}-hint`} className="text-caption text-ink-3">
          {serverNamesReviewer ? "Each reviewer has a personal token: the service records the name bound to your token with each decision and in the audit log." : "Your name is recorded with each decision and in the audit log."} The token is kept in this tab&rsquo;s memory only: not stored in the browser, not put in the address, and gone when you reload. The service checks it when you record a decision.
        </p>
      ) : !serverNamesReviewer && !reviewer.trim() ? (
        <p className="text-caption text-ink-3">Enter your name to enable the decision buttons.</p>
      ) : null}
    </div>
  );
}
