"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useTransition } from "react";

export function SearchInput() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [, startTransition] = useTransition();

  const handleChange = (value: string) => {
    startTransition(() => {
      const params = new URLSearchParams(searchParams.toString());
      if (value) {
        params.set("q", value);
      } else {
        params.delete("q");
      }
      router.push(`?${params.toString()}`, { scroll: false });
    });
  };

  return (
    <input
      type="text"
      placeholder="Search loops..."
      defaultValue={searchParams.get("q") || ""}
      onChange={(e) => handleChange(e.target.value)}
      className="w-full px-3 py-1.5 text-sm border rounded-md bg-background"
    />
  );
}
