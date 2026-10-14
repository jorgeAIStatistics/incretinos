import type { Metadata } from "next";
import type { ReactNode } from "react";

// The page in this folder is a client component and cannot export metadata, so its document title is set here.
export const metadata: Metadata = { title: "Comparison Laboratory" };

export default function Layout({ children }: { children: ReactNode }) {
  return children;
}
