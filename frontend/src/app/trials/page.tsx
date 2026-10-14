import type { Metadata } from "next";
import { Suspense } from "react";
import { Loading } from "@/components/ui";
import { TrialAtlas } from "./TrialAtlas";

export const metadata: Metadata = { title: "Clinical Trial Atlas" };

export default function Page() {
  return (
    <Suspense fallback={<Loading rows={8} />}>
      <TrialAtlas />
    </Suspense>
  );
}
