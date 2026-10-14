"use client";

import { useEffect } from "react";
import { ErrorState, PageHeader } from "@/components/ui";

// Route-level error boundary: the shell and navigation stay; only the page area is replaced.
export default function RouteError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);
  return (
    <>
      <PageHeader title="This page could not be displayed" lede="An unexpected error stopped the page from rendering. No evidence was changed." />
      <ErrorState title="Rendering failed" message={`${error.message || "Unknown error."} Try again; if it keeps failing, reload the page or open another section from the navigation.`} onRetry={reset} />
    </>
  );
}
