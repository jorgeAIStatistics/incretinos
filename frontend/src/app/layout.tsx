import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Shell } from "@/components/Shell";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "IncretinOS — clinical evidence intelligence", template: "%s · IncretinOS" },
  description: "Evidence, comparability and gap analysis for incretin and amylin therapeutics, built on public data. Research use only; not medical advice.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
