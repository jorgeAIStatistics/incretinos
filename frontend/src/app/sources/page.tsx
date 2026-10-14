import type { Metadata } from "next";
import { Suspense } from "react";
import { Loading } from "@/components/ui";
import { SourcesExplorer } from "./SourcesExplorer";

export const metadata: Metadata = { title: "Sources & Audit" };

export default function Page() {
  return (
    <Suspense fallback={<Loading rows={8} />}>
      <SourcesExplorer />
    </Suspense>
  );
}
