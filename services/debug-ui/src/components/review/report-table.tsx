import type { DailyReviewItem } from "@/lib/queries/daily-review";

/**
 * Compact table of correct items for a section of the report.
 *
 * For Part 1 (create_loop) shows: candidate / client.
 * For Part 2 (decisions) shows: action type / loop title / summary.
 */
export function ReportTable({
  items,
  variant,
}: {
  items: DailyReviewItem[];
  variant: "creation" | "decisions";
}) {
  if (items.length === 0) {
    return (
      <div className="text-xs italic text-muted-foreground">
        No correct items.
      </div>
    );
  }

  return (
    <table className="w-full text-sm border-collapse">
      <thead>
        <tr style={{ background: "var(--color-brand-purple)", color: "white" }}>
          {variant === "creation" ? (
            <>
              <Th>Candidate</Th>
              <Th>Client</Th>
              <Th>Recruiter</Th>
            </>
          ) : (
            <>
              <Th>Action</Th>
              <Th>Loop</Th>
              <Th>Summary</Th>
            </>
          )}
        </tr>
      </thead>
      <tbody>
        {items.map((it, idx) => (
          <tr
            key={it.id}
            style={{
              background:
                idx % 2 === 0
                  ? "white"
                  : "var(--color-brand-grey-light)",
            }}
          >
            {variant === "creation" ? (
              <>
                <Td>{it.candidate_name ?? extracted(it, "candidate_name")}</Td>
                <Td>
                  {it.client_company ??
                    extracted(it, "client_company") ??
                    extracted(it, "client_name")}
                </Td>
                <Td>{it.recruiter_name ?? extracted(it, "recruiter_name")}</Td>
              </>
            ) : (
              <>
                <Td>{it.action}</Td>
                <Td>{it.loop_title ?? "—"}</Td>
                <Td className="text-xs">{it.summary}</Td>
              </>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="text-left text-xs font-semibold uppercase tracking-wide px-3 py-2">
      {children}
    </th>
  );
}

function Td({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return <td className={`px-3 py-2 ${className ?? ""}`}>{children ?? "—"}</td>;
}

function extracted(it: DailyReviewItem, key: string): string | null {
  const data = (it.action_data ?? {}) as Record<string, string | null>;
  return data[key] ?? null;
}
