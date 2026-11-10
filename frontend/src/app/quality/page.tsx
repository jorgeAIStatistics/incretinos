import type { Metadata } from "next";
import { Suspense } from "react";
import { QualityDashboard } from "@/components/quality/QualityDashboard";
import { Loading } from "@/components/ui";

export const metadata: Metadata = { title: "Evidence Quality" };

export default function Page() {
  return (
    <Suspense fallback={<Loading rows={10} />}>
      <QualityDashboard />
    </Suspense>
  );
}
