/**
 * Shared interface primitives. Import from "@/components/ui".
 * docs/design-system.md describes when to use each one and the rules they encode.
 */
export { Badge, ClassificationBadge, Hint, RegulatoryBadge, type Tone, toneOf } from "./badge";
export { Button, type ButtonProps, type ButtonSize, type ButtonVariant, buttonClass, IconButton, LinkButton } from "./button";
export { EpistemicBadge, Missing, Qualifiers, TierBadge, VerificationBadge, VerificationIcon } from "./evidence";
export { NumberField, SearchField, SecretField, SegmentedControl, Select, Slider, TextArea, TextField } from "./field";
export { Callout, Disclosure, Hypothetical, KV, Meter, PageHeader, Panel, Section, Stat, StatTile } from "./layout";
export { Ledger, LedgerItem, NextLink, Timeline, TimelineItem } from "./ledger";
export { Drawer, Modal } from "./overlay";
export { SourceLine, ValueCell } from "./provenance";
export { Async, EmptyState, ErrorBoundary, ErrorNote, ErrorState, Loading, Skeleton, TableSkeleton } from "./states";
export { type Column, DataTable, type Density, Pagination, type SortDirection, SortHeader, type SortState, sortRows, TableCard, useSort } from "./table";
export { type TabItem, TabPanel, Tabs } from "./tabs";
export { Term } from "./term";
export { ToastProvider, type ToastTone, useToast } from "./toast";
export type { Label, SourceRef } from "@/lib/types";
