"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

export function LeftPanel({
  coordinatorEmail,
  children,
}: {
  coordinatorEmail: string;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const base = `/${encodeURIComponent(coordinatorEmail)}`;
  const isLoops = pathname.includes("/loops");
  const isReview = pathname.includes("/review");
  const isPending = !isLoops && !isReview;

  const tabClass = (active: boolean) =>
    cn(
      "px-3 py-1.5 text-sm font-medium rounded-md transition-colors",
      active
        ? "bg-primary text-primary-foreground"
        : "text-muted-foreground hover:text-foreground hover:bg-muted"
    );

  return (
    <div className="flex flex-col h-full">
      <div
        className="flex border-b px-4 py-2 gap-1 shrink-0"
        data-print-hide
      >
        <Link href={base} className={tabClass(isPending)}>
          Pending Tasks
        </Link>
        <Link href={`${base}/loops`} className={tabClass(isLoops)}>
          Loops
        </Link>
        <Link href={`${base}/review`} className={tabClass(isReview)}>
          Daily Review
        </Link>
      </div>
      {children}
    </div>
  );
}
