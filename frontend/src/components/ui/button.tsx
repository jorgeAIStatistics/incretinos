"use client";

import clsx from "clsx";
import { LoaderCircle, type LucideIcon } from "lucide-react";
import Link from "next/link";
import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "quiet" | "danger";
export type ButtonSize = "sm" | "md";

/*
 * States, in every variant: hover (one step darker), active (pressed 1px), focus-visible (global 2px ring),
 * disabled (55% opacity, no pointer), loading (spinner, aria-busy, not clickable).
 * Only colour, border, shadow and transform transition. Never `transition: all`.
 */
const BASE =
  "inline-flex shrink-0 select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-control font-medium no-underline hover:no-underline " +
  "transition-[background-color,border-color,color,box-shadow,transform] duration-(--dur-fast) ease-standard active:translate-y-px " +
  "disabled:cursor-not-allowed disabled:opacity-55 disabled:active:translate-y-0 aria-disabled:cursor-not-allowed aria-disabled:opacity-55";

const VARIANT: Record<ButtonVariant, string> = {
  primary: "bg-accent text-white hover:bg-accent-hover active:bg-accent-active",
  secondary: "border border-control bg-surface text-ink hover:bg-hover active:bg-sunken",
  ghost: "border border-control bg-surface text-ink hover:bg-hover active:bg-sunken", // earlier name of "secondary"
  quiet: "text-accent hover:bg-accent-soft active:bg-accent-soft",
  danger: "border border-critical bg-surface text-critical hover:bg-critical-soft active:bg-critical-soft",
};

const SIZE: Record<ButtonSize, string> = {
  md: "h-(--control-h) px-3.5 text-body",
  sm: "h-(--control-h-sm) px-2.5 text-data",
};

export const buttonClass = (variant: ButtonVariant = "primary", size: ButtonSize = "md", className?: string) => clsx(BASE, VARIANT[variant], SIZE[size], className);

type Common = { children: ReactNode; variant?: ButtonVariant; size?: ButtonSize; className?: string; Icon?: LucideIcon };

export type ButtonProps = Common & {
  onClick?: () => void;
  disabled?: boolean;
  /** A request started by this button is running: shows a spinner and ignores further clicks. */
  loading?: boolean;
  type?: "button" | "submit";
} & Pick<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label" | "aria-expanded" | "aria-controls" | "aria-pressed" | "aria-describedby" | "title" | "form" | "name" | "value" | "id">;

export function Button({ children, onClick, variant = "primary", size = "md", disabled, loading, type = "button", className, Icon, ...rest }: ButtonProps) {
  return (
    <button type={type} onClick={loading ? undefined : onClick} disabled={disabled} aria-busy={loading || undefined} className={buttonClass(variant, size, className)} {...rest}>
      {loading ? <LoaderCircle aria-hidden size={size === "sm" ? 13 : 15} className="spin" /> : Icon ? <Icon aria-hidden size={size === "sm" ? 13 : 15} /> : null}
      {children}
    </button>
  );
}

/** Navigation that looks like a button. Use a link for navigation and a button for an action, never the reverse. */
export function LinkButton({ children, href, variant = "primary", size = "md", className, Icon, external, download }: Common & { href: string; external?: boolean; download?: boolean }) {
  const content = (
    <>
      {Icon ? <Icon aria-hidden size={size === "sm" ? 13 : 15} /> : null}
      {children}
    </>
  );
  if (external || download) {
    return (
      <a href={href} className={buttonClass(variant, size, className)} {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})} {...(download ? { download: true } : {})}>
        {content}
      </a>
    );
  }
  return (
    <Link href={href} className={buttonClass(variant, size, className)}>
      {content}
    </Link>
  );
}

/** A button with an icon and no visible text. `label` is required: it is the accessible name. */
export function IconButton({ label, Icon, onClick, size = "md", className, disabled, ...rest }: { label: string; Icon: LucideIcon; onClick?: () => void; size?: ButtonSize; className?: string; disabled?: boolean } & Pick<ButtonHTMLAttributes<HTMLButtonElement>, "aria-expanded" | "aria-controls" | "aria-pressed" | "aria-haspopup" | "id">) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      className={clsx(BASE, "text-ink-2 hover:bg-hover hover:text-ink active:bg-sunken", size === "sm" ? "size-(--control-h-sm)" : "size-(--control-h)", className)}
      {...rest}
    >
      <Icon aria-hidden size={size === "sm" ? 15 : 18} />
    </button>
  );
}
