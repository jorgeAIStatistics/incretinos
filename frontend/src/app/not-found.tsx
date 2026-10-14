import type { Metadata } from "next";
import { EmptyState, LinkButton, PageHeader } from "@/components/ui";

export const metadata: Metadata = { title: { absolute: "Page not found · IncretinOS" } };

export default function NotFound() {
  return (
    <>
      <PageHeader title="Page not found" lede="There is no page at this address." />
      <EmptyState title="Nothing lives here" action={<LinkButton href="/" variant="secondary">Go to the Command Center</LinkButton>}>
        The link may be out of date, or the address may have been mistyped. Use the navigation to reach any section.
      </EmptyState>
    </>
  );
}
