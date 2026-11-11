import type { Metadata } from "next";
import { Suspense } from "react";
import { Loading } from "@/components/ui";
import { EstimandInspector } from "./EstimandInspector";

export const metadata: Metadata = { title: "Estimand Inspector" };

export default function Page() {
  return (
    <Suspense fallback={<Loading rows={8} />}>
      <EstimandInspector />
    </Suspense>
  );
}
