"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";

/**
 * Simple date picker that pushes the user to /[coord]/review/[date].
 * Used on the review landing page for jumping to a specific day.
 */
export function DatePicker({
  coordinatorEmail,
  defaultDate,
}: {
  coordinatorEmail: string;
  defaultDate: string;
}) {
  const router = useRouter();
  const [value, setValue] = useState(defaultDate);

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
          router.push(
            `/${encodeURIComponent(coordinatorEmail)}/review/${value}`
          );
        }
      }}
      className="flex items-center gap-2"
    >
      <input
        type="date"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="h-8 rounded-md border border-input bg-background px-2 text-sm"
      />
      <Button type="submit" size="sm">
        Open
      </Button>
    </form>
  );
}
