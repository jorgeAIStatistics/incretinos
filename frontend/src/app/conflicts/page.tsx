import type { Metadata } from "next";
import { Suspense } from "react";
import { ConflictsView } from "@/components/conflicts/ConflictsView";
import { Loading } from "@/components/ui";

export const metadata: Metadata = { title: "Conflicts" };

export default function Page() {
  return (
    <Suspense fallback={<Loading rows={8} />}>
      <ConflictsView />
    </Suspense>
  );
}
