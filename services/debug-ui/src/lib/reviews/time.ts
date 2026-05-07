/**
 * Eastern-Time day-window helpers.
 *
 * The user reviews "what the agent did yesterday in ET" — but Postgres
 * stores `created_at` in UTC. So we need to translate a YYYY-MM-DD ET
 * date into a [start, end) UTC range for the SQL filter.
 *
 * We avoid date-fns / luxon: the standard `Intl.DateTimeFormat` API
 * already knows EST vs EDT for any given instant. The trick is a
 * one-step correction: guess UTC midnight against EST (-5h), then read
 * back the wall hour in ET. If we land on hour 1, we're inside DST and
 * need to subtract another hour to hit true ET midnight.
 */

/**
 * Returns [startUtcIso, endUtcIso) for the given YYYY-MM-DD date treated
 * as a calendar day in America/New_York.
 *
 * Half-open interval matches SQL `created_at >= $start AND created_at < $end`.
 */
export function etDayToUtcRange(date: string): [string, string] {
  const start = etMidnightUtcIso(date);
  const end = etMidnightUtcIso(addDaysISO(date, 1));
  return [start, end];
}

/** Add `n` calendar days to a YYYY-MM-DD string (UTC date arithmetic, no TZ). */
export function addDaysISO(date: string, n: number): string {
  const d = new Date(date + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

/** Today's date in ET as YYYY-MM-DD. */
export function todayInET(): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const get = (type: string) =>
    parts.find((p) => p.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

/** Yesterday's date in ET as YYYY-MM-DD. */
export function yesterdayInET(): string {
  return addDaysISO(todayInET(), -1);
}

/**
 * Convert "midnight wall time on `date` in ET" → a UTC ISO string.
 *
 * Algorithm: guess UTC = date 05:00Z (midnight EST). Check what hour
 * that is in ET. If it's 0, we're correct. If it's 1, we're in EDT and
 * need to back off an hour.
 */
function etMidnightUtcIso(date: string): string {
  const [y, m, d] = date.split("-").map(Number);
  const guess = new Date(Date.UTC(y, m - 1, d, 5, 0, 0));
  const hourInET = parseInt(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      hourCycle: "h23",
    }).format(guess),
    10
  );
  // hourInET will be 0 (winter, our guess was right) or 1 (summer, back off 1h).
  const corrected = new Date(guess.getTime() - hourInET * 3600 * 1000);
  return corrected.toISOString();
}

/** Format a YYYY-MM-DD date as a human-readable label in ET. */
export function formatETDate(date: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(date + "T12:00:00Z")); // mid-day to dodge any DST edges
}
