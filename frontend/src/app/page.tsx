import type { Metadata } from "next";
import { CommandCenter } from "./CommandCenter";

// The root layout's title template does not apply to a page in its own segment, so the full title is given here.
export const metadata: Metadata = { title: { absolute: "Command Center · IncretinOS" } };

export default function Page() {
  return <CommandCenter />;
}
