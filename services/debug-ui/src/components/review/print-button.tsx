"use client";

import { Button } from "@/components/ui/button";

/**
 * Triggers the browser's print dialog. The user picks "Save as PDF" as
 * the destination. Print rules in `globals.css` hide everything tagged
 * `data-print-hide` (including this button itself).
 */
export function PrintButton() {
  return (
    <Button
      onClick={() => window.print()}
      data-print-hide
      style={{ background: "var(--color-brand-purple)", color: "white" }}
    >
      Print Report
    </Button>
  );
}
