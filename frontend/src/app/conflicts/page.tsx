import type { Metadata } from "next";
import { EmptyState, LinkButton, PageHeader } from "@/components/ui";

export const metadata: Metadata = { title: "Conflicts" };

// Placeholder: this page is built later in this release. It exists now so the navigation does not change again.
export default function Page() {
  return (
    <>
      <PageHeader eyebrow="Review and provenance" title="Conflicts" lede="Where two public sources disagree about the same quantity, shown side by side with both sources kept." />
      <EmptyState title="This page is not available yet" action={<LinkButton href="/gaps?nature=SOURCE_CONFLICT" variant="secondary" className="!h-auto min-h-(--control-h) max-w-full !shrink py-1.5 text-left !whitespace-normal">Open source conflicts in the gap tracker</LinkButton>}>
        It arrives later in this release. Until then, source disagreements appear in the Evidence Gap Tracker under the reason Source conflict.
      </EmptyState>
    </>
  );
}
