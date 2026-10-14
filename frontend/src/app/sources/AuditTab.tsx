"use client";

import { type Column, DataTable, Disclosure, EmptyState, ErrorState, Pagination, TableSkeleton } from "@/components/ui";
import { usePagedList } from "@/lib/paging";
import type { AuditEvent } from "@/lib/types";

/** Detail payloads are shown in full and wrap; a long one sits behind a disclosure rather than being cut off. */
function Detail({ value }: { value: unknown }) {
  if (value === null || value === undefined || (typeof value === "object" && Object.keys(value as object).length === 0)) return <span className="text-ink-3">No detail recorded</span>;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 1).replace(/\n\s*/g, " ");
  if (text.length <= 160) return <span className="mono break-all text-ink-2">{text}</span>;
  return (
    <Disclosure summary={`Show detail (${text.length.toLocaleString("en-US")} characters)`}>
      <pre className="mono max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-control bg-sunken p-2 text-ink-2">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>
    </Disclosure>
  );
}

const COLUMNS: Column<AuditEvent>[] = [
  { key: "at", header: "Time (UTC)", className: "whitespace-nowrap", cell: (e) => e.at.slice(0, 19).replace("T", " ") },
  { key: "actor", header: "Actor", className: "whitespace-nowrap", cell: (e) => e.actor },
  { key: "action", header: "Action", rowHeader: true, className: "mono whitespace-nowrap", cell: (e) => <span translate="no">{e.action}</span> },
  {
    key: "target",
    header: "Target",
    className: "mono whitespace-nowrap",
    cell: (e) => (e.target_type ? <span translate="no">{`${e.target_type}${e.target_id !== null && e.target_id !== undefined && e.target_id !== "" ? `:${e.target_id}` : ""}`}</span> : <span className="font-sans text-ink-3">None</span>),
  },
  { key: "detail", header: "Detail", className: "min-w-[280px] max-w-xl", cell: (e) => <Detail value={e.detail} /> },
];

export function AuditTab() {
  const log = usePagedList<AuditEvent>("/audit", { pageSize: 50, legacyLimit: 1000 });
  if (log.status === "error") return <ErrorState title="The audit log could not be loaded" message={log.error ?? ""} onRetry={log.reload} />;
  if (!log.data) return <TableSkeleton rows={10} columns={5} label="Loading the audit log…" />;
  return (
    <DataTable
      label="Audit log, newest first"
      columns={COLUMNS}
      rows={log.data.rows}
      rowKey={(e) => e.id}
      density="compact"
      refreshing={log.refreshing}
      minWidth={860}
      toolbar={<p>Actions recorded by the service, newest first.</p>}
      empty={<EmptyState title="The audit log is empty" className="m-3 border-0">Actions are recorded as they happen.</EmptyState>}
      footer={<Pagination page={log.page} pageSize={log.pageSize} total={log.data.total} reachable={log.data.reachable} truncated={log.data.truncated} onPage={log.setPage} onPageSize={log.setPageSize} noun="audit events" />}
    />
  );
}
