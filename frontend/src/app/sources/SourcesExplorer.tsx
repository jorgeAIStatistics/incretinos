"use client";

import { useState } from "react";
import { PageHeader, TabPanel, Tabs } from "@/components/ui";
import { useQueryParams } from "@/lib/hooks";
import { AuditTab } from "./AuditTab";
import { ChangesTab, type Reviewer } from "./ChangesTab";
import { SourcesTab } from "./SourcesTab";
import { ValidationTab } from "./ValidationTab";

const TABS = [
  { value: "sources", label: "Sources" },
  { value: "changes", label: "Changes and review queue" },
  { value: "audit", label: "Audit log" },
  { value: "validation", label: "Validation rules" },
];

export function SourcesExplorer() {
  const [param, setParams] = useQueryParams();
  const requested = param("tab", "sources");
  const tab = TABS.some((t) => t.value === requested) ? requested : "sources";
  // Reviewer name and token live here, above the tabs, so switching tabs does not discard them. They are held in
  // memory only: never written to storage, a cookie or the address bar, and gone on reload.
  const [reviewer, setReviewer] = useState<Reviewer>({ name: "", token: "" });
  return (
    <>
      <PageHeader
        eyebrow="Source and audit explorer"
        title="Where every number came from, and what has changed"
        lede="Sources are hashed and versioned, curated quotes are anchored to source text, changes are queued for review, and every action is logged."
      />
      <Tabs label="Provenance views" value={tab} onValueChange={(v) => setParams({ tab: v === "sources" ? null : v })} items={TABS}>
        <TabPanel value="sources">
          <SourcesTab />
        </TabPanel>
        <TabPanel value="changes">
          <ChangesTab reviewer={reviewer} onReviewer={setReviewer} />
        </TabPanel>
        <TabPanel value="audit">
          <AuditTab />
        </TabPanel>
        <TabPanel value="validation">
          <ValidationTab />
        </TabPanel>
      </Tabs>
    </>
  );
}
