/**
 * Hero stat boxes for the report. Big numbers in brand purple, designed
 * to "pop" both on screen and in print. The boxes carry
 * data-print-keep-together so they don't break across pages.
 */
export function ReportStats({
  correct,
  incorrect,
  unreviewed,
}: {
  correct: number;
  incorrect: number;
  unreviewed: number;
}) {
  return (
    <div className="grid grid-cols-3 gap-4" data-print-keep-together>
      <Stat n={correct} label="Correct" />
      <Stat n={incorrect} label="Incorrect" emphasized={incorrect > 0} />
      <Stat n={unreviewed} label="Unreviewed" muted />
    </div>
  );
}

function Stat({
  n,
  label,
  emphasized,
  muted,
}: {
  n: number;
  label: string;
  emphasized?: boolean;
  muted?: boolean;
}) {
  // Inline styles so print color-adjust applies without Tailwind purging.
  const numberColor = muted
    ? "var(--color-brand-grey)"
    : "var(--color-brand-purple)";
  const borderColor = emphasized
    ? "var(--color-brand-purple)"
    : "var(--color-brand-grey-light)";
  return (
    <div
      className="rounded-lg p-6 text-center"
      style={{
        background: "var(--color-brand-grey-light)",
        border: `2px solid ${borderColor}`,
      }}
    >
      <div
        className="text-6xl font-bold leading-none"
        style={{ color: numberColor }}
      >
        {n}
      </div>
      <div
        className="text-xs uppercase tracking-wider mt-2 font-semibold"
        style={{ color: "var(--color-brand-black)" }}
      >
        {label}
      </div>
    </div>
  );
}
