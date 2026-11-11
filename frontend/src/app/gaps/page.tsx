import type { Metadata } from "next";
import { Suspense } from "react";
import { Loading } from "@/components/ui";
import { GapTracker } from "./GapTracker";

export const metadata: Metadata = { title: "Evidence Gap Tracker" };

export default function Page() {
  return (
    <Suspense fallback={<Loading rows={8} />}>
      <GapTracker />
    </Suspense>
  );
}
