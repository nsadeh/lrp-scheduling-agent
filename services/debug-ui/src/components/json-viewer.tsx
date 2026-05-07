"use client";

import { useState } from "react";

export function JsonViewer({
  data,
  label,
}: {
  data: unknown;
  label?: string;
}) {
  const [open, setOpen] = useState(false);

  if (data === null || data === undefined) return null;

  const isEmpty =
    typeof data === "object" && Object.keys(data as object).length === 0;
  if (isEmpty) return null;

  return (
    <div className="text-xs">
      <button
        onClick={() => setOpen(!open)}
        className="text-muted-foreground hover:text-foreground flex items-center gap-1"
      >
        <span>{open ? "▾" : "▸"}</span>
        <span>{label || "JSON"}</span>
      </button>
      {open && (
        <pre className="mt-1 p-2 bg-muted rounded text-xs overflow-x-auto max-h-60">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}
