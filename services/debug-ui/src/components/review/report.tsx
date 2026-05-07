import type { DailyReviewItem } from "@/lib/queries/daily-review";
import type { DayReviewFile, ReviewEntry } from "@/lib/reviews/types";
import { ReportStats } from "./report-stats";
import { ReportTable } from "./report-table";
import { ReportIncorrect } from "./report-incorrect";

/**
 * The full daily-review report — designed to render at letter size with
 * Cmd+P → Save as PDF. Only uses inline brand colors so print fidelity
 * is exact regardless of OKLCH support.
 *
 * Sections:
 *   1. Hero (date, coordinator)
 *   2. Part 1 — Loop Creation: stats + correct table + incorrect entries
 *   3. Part 2 — Decisions: stats + correct table + incorrect entries
 */
export function Report({
  coordinatorEmail,
  coordinatorName,
  date,
  formattedDate,
  generatedDate,
  items,
  reviews,
}: {
  coordinatorEmail: string;
  coordinatorName: string | null;
  date: string;
  formattedDate: string;
  generatedDate: string;
  items: DailyReviewItem[];
  reviews: DayReviewFile | null;
}) {
  const reviewsById = reviews?.reviews ?? {};

  const part1 = items.filter((it) => it.action === "create_loop");
  const part2 = items.filter((it) => it.action !== "create_loop");

  const split = (its: DailyReviewItem[]) => splitByReview(its, reviewsById);
  const p1 = split(part1);
  const p2 = split(part2);

  return (
    <article
      className="mx-auto max-w-[8.5in] bg-white p-10 space-y-8"
      style={{ color: "var(--color-brand-black)" }}
    >
      {/* Hero */}
      <header
        className="pb-6"
        style={{ borderBottom: "2px solid var(--color-brand-purple)" }}
      >
        <div
          className="text-xs uppercase tracking-widest font-semibold"
          style={{ color: "var(--color-brand-purple)" }}
        >
          Daily Agent Review
        </div>
        <h1 className="text-3xl font-bold mt-1">
          Long Ridge Partners
          {coordinatorName ? ` · ${coordinatorName}` : ""}
        </h1>
        <div
          className="text-sm mt-2"
          style={{ color: "var(--color-brand-grey)" }}
        >
          {formattedDate} (Eastern Time) · Generated {generatedDate}
          {coordinatorName ? ` · ${coordinatorEmail}` : ""}
        </div>
      </header>

      {/* Part 1 — Loop Creation */}
      <section className="space-y-4" data-print-keep-together>
        <SectionHeading
          number="01"
          title="Loop Creation"
          subtitle="The agent extracted candidate, client, recruiter, and CM identities from inbound emails."
        />
        <ReportStats
          correct={p1.correct.length}
          incorrect={p1.incorrect.length}
          unreviewed={p1.unreviewed.length}
        />
      </section>

      <section className="space-y-3">
        <SubHeading>Correct extractions</SubHeading>
        <ReportTable items={p1.correct} variant="creation" />
      </section>

      <section className="space-y-3">
        <SubHeading>Incorrect extractions</SubHeading>
        <ReportIncorrect pairs={p1.incorrect} />
      </section>

      {/* Part 2 — Decisions */}
      <section
        className="space-y-4 pt-4"
        data-print-keep-together
        style={{ borderTop: "1px solid var(--color-brand-grey-light)" }}
      >
        <SectionHeading
          number="02"
          title="Decisions"
          subtitle="Email drafts and questions surfaced for coordinator approval."
        />
        <ReportStats
          correct={p2.correct.length}
          incorrect={p2.incorrect.length}
          unreviewed={p2.unreviewed.length}
        />
      </section>

      <section className="space-y-3">
        <SubHeading>Correct decisions</SubHeading>
        <ReportTable items={p2.correct} variant="decisions" />
      </section>

      <section className="space-y-3">
        <SubHeading>Incorrect decisions</SubHeading>
        <ReportIncorrect pairs={p2.incorrect} />
      </section>

      <footer
        className="text-[10px] uppercase tracking-widest pt-6"
        style={{ color: "var(--color-brand-grey)" }}
      >
        Kinematic Labs · {date}
      </footer>
    </article>
  );
}

function SectionHeading({
  number,
  title,
  subtitle,
}: {
  number: string;
  title: string;
  subtitle?: string;
}) {
  return (
    <div>
      <div
        className="text-[10px] font-semibold uppercase tracking-widest"
        style={{ color: "var(--color-brand-purple)" }}
      >
        Part {number}
      </div>
      <h2 className="text-2xl font-bold mt-0.5">{title}</h2>
      {subtitle && (
        <p
          className="text-sm mt-1"
          style={{ color: "var(--color-brand-grey)" }}
        >
          {subtitle}
        </p>
      )}
    </div>
  );
}

function SubHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3
      className="text-xs font-semibold uppercase tracking-wider"
      style={{ color: "var(--color-brand-grey)" }}
    >
      {children}
    </h3>
  );
}

function splitByReview(
  items: DailyReviewItem[],
  reviewsById: Record<string, ReviewEntry>
) {
  const correct: DailyReviewItem[] = [];
  const incorrect: { item: DailyReviewItem; review: ReviewEntry }[] = [];
  const unreviewed: DailyReviewItem[] = [];

  for (const item of items) {
    const r = reviewsById[item.id];
    if (!r) unreviewed.push(item);
    else if (r.correct) correct.push(item);
    else incorrect.push({ item, review: r });
  }

  return { correct, incorrect, unreviewed };
}
