import Link from "next/link";
import { getDailyReviewCounts } from "@/lib/queries/daily-review";
import { readDayReview } from "@/lib/reviews/storage";
import {
  addDaysISO,
  formatETDate,
  todayInET,
  yesterdayInET,
} from "@/lib/reviews/time";
import { DatePicker } from "@/components/review/date-picker";

/**
 * Daily review landing page.
 *
 * Shows the last 14 ET days for this coordinator with item counts,
 * review progress, and a date picker for older days.
 */
export default async function ReviewLandingPage({
  params,
}: {
  params: Promise<{ coordinatorEmail: string }>;
}) {
  const { coordinatorEmail: rawEmail } = await params;
  const coordinatorEmail = decodeURIComponent(rawEmail);

  const today = todayInET();
  const days: string[] = [];
  for (let n = 0; n < 14; n++) days.push(addDaysISO(today, -n));

  const [counts, reviews] = await Promise.all([
    getDailyReviewCounts(coordinatorEmail, days),
    Promise.all(days.map((d) => readDayReview(coordinatorEmail, d))),
  ]);

  const cards = days.map((date, i) => {
    const total = counts.get(date) ?? 0;
    const dayReview = reviews[i];
    const reviewedIds = Object.keys(dayReview?.reviews ?? {});
    const reviewed = reviewedIds.length;
    const incorrect = Object.values(dayReview?.reviews ?? {}).filter(
      (r) => !r.correct
    ).length;
    return { date, total, reviewed, incorrect };
  });

  return (
    <div className="overflow-y-auto h-full">
      <div className="p-6 max-w-4xl mx-auto space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Daily Review</h1>
          <p className="text-sm text-muted-foreground">
            Audit the agent&apos;s loop creations and decisions for{" "}
            {coordinatorEmail}, one day at a time.
          </p>
        </div>
        <DatePicker
          coordinatorEmail={coordinatorEmail}
          defaultDate={yesterdayInET()}
        />
      </header>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wide">
          Last 14 days (Eastern Time)
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {cards.map((c) => (
            <DayCard
              key={c.date}
              coordinatorEmail={coordinatorEmail}
              date={c.date}
              total={c.total}
              reviewed={c.reviewed}
              incorrect={c.incorrect}
              isToday={c.date === today}
            />
          ))}
        </div>
      </section>
      </div>
    </div>
  );
}

function DayCard({
  coordinatorEmail,
  date,
  total,
  reviewed,
  incorrect,
  isToday,
}: {
  coordinatorEmail: string;
  date: string;
  total: number;
  reviewed: number;
  incorrect: number;
  isToday: boolean;
}) {
  const empty = total === 0;
  return (
    <Link
      href={`/${encodeURIComponent(coordinatorEmail)}/review/${date}`}
      className="group border rounded-lg p-4 hover:bg-muted/40 transition-colors block"
    >
      <div className="flex items-baseline justify-between mb-2">
        <div className="text-sm font-medium">
          {formatETDate(date)}
          {isToday && (
            <span className="ml-2 text-xs text-muted-foreground">(today)</span>
          )}
        </div>
        <div className="text-xs text-muted-foreground group-hover:text-foreground">
          Open →
        </div>
      </div>
      {empty ? (
        <div className="text-xs text-muted-foreground italic">
          No reviewable suggestions
        </div>
      ) : (
        <div className="flex gap-4 text-sm">
          <div>
            <div className="text-2xl font-semibold">{total}</div>
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              items
            </div>
          </div>
          <div>
            <div className="text-2xl font-semibold">
              {reviewed}/{total}
            </div>
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              reviewed
            </div>
          </div>
          <div>
            <div
              className={
                incorrect > 0
                  ? "text-2xl font-semibold text-destructive"
                  : "text-2xl font-semibold text-muted-foreground"
              }
            >
              {incorrect}
            </div>
            <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
              incorrect
            </div>
          </div>
        </div>
      )}
    </Link>
  );
}
