"use client";

import { useState } from "react";

/**
 * Mono strip of trace-able IDs for the current item — clickable to copy.
 * Used at the top of each review item so the reviewer can paste the
 * IDs into Langfuse / Sentry / DB queries.
 */
export function ItemIds({
  ids,
}: {
  ids: { label: string; value: string | null | undefined }[];
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
      {ids.map((id) =>
        id.value ? (
          <CopyableId key={id.label} label={id.label} value={id.value} />
        ) : null
      )}
    </div>
  );
}

function CopyableId({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        } catch {
          // clipboard blocked — silent
        }
      }}
      title="Click to copy"
      className="inline-flex items-baseline gap-1.5 font-mono text-muted-foreground hover:text-foreground transition-colors"
    >
      <span className="uppercase text-[9px] tracking-wide opacity-70">
        {label}
      </span>
      <span className="select-all">{value}</span>
      <span className="text-[9px] opacity-60">
        {copied ? "✓ copied" : "📋"}
      </span>
    </button>
  );
}
