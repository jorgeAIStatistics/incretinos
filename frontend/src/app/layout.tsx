import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { Shell } from "@/components/Shell";
import "./globals.css";

// Every route sets its own title through this template (a page's `metadata`, or a layout for client-only routes).
export const metadata: Metadata = {
  title: { default: "IncretinOS · Clinical evidence intelligence", template: "%s · IncretinOS" },
  description: "Evidence, comparability and gap analysis for incretin and amylin therapeutics, built on public data. Research use only; not medical advice.",
  applicationName: "IncretinOS",
};

// Matches --page. Light is the only validated theme, so the browser chrome is told to stay light as well.
export const viewport: Viewport = { themeColor: "#f6f7f8", colorScheme: "light", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
