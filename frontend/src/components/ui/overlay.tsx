"use client";

import * as Dialog from "@radix-ui/react-dialog";
import clsx from "clsx";
import { X } from "lucide-react";
import { type ReactNode, useRef } from "react";
import { IconButton } from "./button";

/*
 * Modal and drawer, both on the Radix dialog primitive: focus is trapped inside while open, Escape and the scrim
 * close it, focus returns to the element that opened it, the page behind is inert and does not scroll.
 * Motion: the scrim fades and the panel moves a few pixels (modal) or slides from its edge (drawer), 220 ms in
 * and 160 ms out; under reduced motion both appear at once.
 */

const SCRIM = "overlay-scrim fixed inset-0 z-(--z-overlay) bg-scrim";

/**
 * Focus return. Radix restores focus only to its own <Dialog.Trigger>; these dialogs are opened by ordinary
 * buttons and links, so the element that had focus when the dialog opened is remembered and focused again on close.
 */
function useFocusReturn() {
  const opener = useRef<HTMLElement | null>(null);
  return {
    onOpenAutoFocus: () => {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    },
    onCloseAutoFocus: (event: Event) => {
      event.preventDefault();
      if (opener.current?.isConnected) opener.current.focus();
    },
  };
}

export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  size = "md",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  /** Actions, right-aligned. Put the primary action last. */
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
}) {
  const focusReturn = useFocusReturn();
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={SCRIM} />
        <Dialog.Content
          className={clsx(
            "modal-panel fixed inset-0 z-(--z-popover) m-auto flex h-fit max-h-[min(88dvh,720px)] w-[calc(100vw-2rem)] flex-col overscroll-contain rounded-overlay border border-hairline bg-surface shadow-overlay",
            size === "sm" ? "max-w-sm" : size === "lg" ? "max-w-2xl" : "max-w-lg",
          )}
          {...(description ? {} : { "aria-describedby": undefined })}
          {...focusReturn}
        >
          <div className="flex items-start justify-between gap-3 px-5 pt-4">
            <div className="min-w-0">
              <Dialog.Title className="text-title font-semibold text-ink">{title}</Dialog.Title>
              {description ? <Dialog.Description className="mt-1 text-data text-ink-2">{description}</Dialog.Description> : null}
            </div>
            <Dialog.Close asChild>
              <IconButton label="Close" Icon={X} size="sm" className="-mr-1.5 -mt-0.5" />
            </Dialog.Close>
          </div>
          {children ? <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-4 text-body">{children}</div> : <div className="h-3" />}
          {footer ? <div className="flex flex-wrap justify-end gap-2 border-t border-hairline px-5 py-3">{footer}</div> : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function Drawer({
  open,
  onOpenChange,
  title,
  children,
  side = "right",
  width = "28rem",
  hideTitle,
  footer,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Always required: it names the dialog. `hideTitle` keeps it for assistive technology only. */
  title: string;
  children: ReactNode;
  side?: "left" | "right";
  /** CSS width; the drawer never exceeds the viewport minus a visible strip of the page. */
  width?: string;
  hideTitle?: boolean;
  footer?: ReactNode;
}) {
  const focusReturn = useFocusReturn();
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={SCRIM} />
        <Dialog.Content
          data-side={side}
          aria-describedby={undefined}
          {...focusReturn}
          className={clsx(
            "drawer-panel fixed inset-y-0 z-(--z-popover) flex max-w-[calc(100vw-3rem)] flex-col overscroll-contain bg-surface shadow-overlay",
            "pb-[env(safe-area-inset-bottom)] pt-[env(safe-area-inset-top)]",
            side === "left" ? "left-0 border-r border-hairline" : "right-0 border-l border-hairline",
          )}
          style={{ width }}
        >
          <div className={clsx("flex items-center justify-between gap-3 px-4", hideTitle ? "absolute right-0 top-[env(safe-area-inset-top)] z-10 h-(--topbar-h)" : "h-(--topbar-h) shrink-0 border-b border-hairline")}>
            <Dialog.Title className={clsx("min-w-0 truncate text-body font-semibold text-ink", hideTitle && "sr-only")}>{title}</Dialog.Title>
            <Dialog.Close asChild>
              <IconButton label="Close" Icon={X} size="sm" />
            </Dialog.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">{children}</div>
          {footer ? <div className="shrink-0 border-t border-hairline px-4 py-3">{footer}</div> : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
