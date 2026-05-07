"use client";

import { useRouter } from "next/navigation";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { CoordinatorOption } from "@/lib/types";

export function CoordinatorSelector({
  coordinators,
  currentEmail,
}: {
  coordinators: CoordinatorOption[];
  currentEmail: string;
}) {
  const router = useRouter();

  return (
    <Select
      value={currentEmail}
      onValueChange={(email) => {
        if (email) router.push(`/${encodeURIComponent(email)}`);
      }}
    >
      <SelectTrigger className="w-[320px]">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {coordinators.map((c) => (
          <SelectItem key={c.email} value={c.email}>
            {c.name ? `${c.name} (${c.email})` : c.email}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
