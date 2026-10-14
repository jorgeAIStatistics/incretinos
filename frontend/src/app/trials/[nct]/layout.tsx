import type { Metadata } from "next";
import type { ReactNode } from "react";

const API_URL = process.env.INCRETINOS_API_URL ?? "http://127.0.0.1:8000";

// The trial page is a client component, so its document title is produced here. The trial's display name is
// read from the API; if the service does not answer quickly the title falls back to the registry identifier.
export async function generateMetadata({ params }: { params: Promise<{ nct: string }> }): Promise<Metadata> {
  const { nct } = await params;
  const id = nct.toUpperCase();
  if (!/^NCT\d{8}$/.test(id)) return { title: "Trial not found" };
  try {
    const res = await fetch(`${API_URL}/api/studies/${id}`, { cache: "no-store", signal: AbortSignal.timeout(1500) });
    if (res.ok) {
      const study: { display_name?: unknown } = await res.json();
      if (typeof study.display_name === "string" && study.display_name) return { title: `${study.display_name} (${id})` };
    }
  } catch {
    /* fall through to the identifier */
  }
  return { title: `Trial ${id}` };
}

export default function Layout({ children }: { children: ReactNode }) {
  return children;
}
