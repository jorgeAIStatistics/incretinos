import type { Metadata } from "next";
import { EmptyState, LinkButton, PageHeader } from "@/components/ui";

export const metadata: Metadata = { title: "Evidence Review" };

// Placeholder: this page is built later in this release. It exists now so the navigation does not change again.
export default function Page() {
  return (
    <>
      <PageHeader eyebrow="Review and provenance" title="Evidence Review" lede="Review each stored evidence record against its source and record an approve, reject or needs-information decision." />
      <EmptyState title="This page is not available yet" action={<LinkButton href="/sources?tab=changes" variant="secondary">Open the review queue</LinkButton>}>
        It arrives later in this release. Until then, open review items are listed under Sources & Audit, in the changes and review queue.
      </EmptyState>
    </>
  );
}
