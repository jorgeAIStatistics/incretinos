import type { Metadata } from "next";
import { Suspense } from "react";
import { ReviewWorkbench } from "@/components/review/ReviewWorkbench";
import { Loading } from "@/components/ui";

export const metadata: Metadata = { title: "Evidence Review" };

export default function Page() {
  return (
    <Suspense fallback={<Loading rows={8} />}>
      <ReviewWorkbench />
    </Suspense>
  );
}
