import type { Metadata } from "next";
import { EmptyState, LinkButton, PageHeader } from "@/components/ui";

export const metadata: Metadata = { title: "Evidence Quality" };

// Placeholder: this page is built later in this release. It exists now so the navigation does not change again.
export default function Page() {
  return (
    <>
      <PageHeader eyebrow="Review and provenance" title="Evidence Quality" lede="How much of the stored evidence has been checked, by whom, and where automated checks flag a risk of misattribution." />
      <EmptyState title="This page is not available yet" action={<LinkButton href="/sources?tab=validation" variant="secondary">Open the validation rules</LinkButton>}>
        It arrives later in this release. Until then, the Command Center shows how the stored results were checked, and Sources & Audit lists the validation rules.
      </EmptyState>
    </>
  );
}
