import type { Metadata } from "next";
import { Suspense } from "react";
import { QualityRecords } from "@/components/quality/QualityRecords";
import { Loading } from "@/components/ui";

export const metadata: Metadata = { title: "Quality Records" };

export default function Page() {
  return (
    <Suspense fallback={<Loading rows={8} />}>
      <QualityRecords />
    </Suspense>
  );
}
