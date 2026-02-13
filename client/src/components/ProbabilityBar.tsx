import { cn } from "@/lib/utils";

function pct(n: number) {
  if (!Number.isFinite(n)) return "—";
  return `${Math.round(n * 100)}%`;
}

export function ProbabilityBar({
  homeLabel,
  awayLabel,
  homeProb,
  awayProb,
  drawProb,
  recommendedPick,
  testId,
}: {
  homeLabel: string;
  awayLabel: string;
  homeProb: number | null | undefined;
  awayProb: number | null | undefined;
  drawProb?: number | null | undefined;
  recommendedPick?: string | null | undefined;
  testId?: string;
}) {
  const h = typeof homeProb === "number" ? Math.max(0, Math.min(1, homeProb)) : 0;
  const a = typeof awayProb === "number" ? Math.max(0, Math.min(1, awayProb)) : 0;
  const d = typeof drawProb === "number" ? Math.max(0, Math.min(1, drawProb)) : 0;

  const sum = h + a + d;
  const hn = sum > 0 ? h / sum : 0;
  const an = sum > 0 ? a / sum : 0;
  const dn = sum > 0 ? d / sum : 0;

  return (
    <div
      data-testid={testId}
      className="rounded-2xl border border-border/60 bg-white/4 p-3"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm font-semibold text-foreground truncate">
          {homeLabel} vs {awayLabel}
        </div>
        {recommendedPick ? (
          <div className="text-[11px] font-bold uppercase tracking-wide text-primary">
            {recommendedPick}
          </div>
        ) : (
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            No pick yet
          </div>
        )}
      </div>

      <div className="mt-2">
        <div className="h-3 w-full overflow-hidden rounded-full bg-black/35 border border-border/50">
          <div className="flex h-full w-full">
            <div
              className="h-full bg-gradient-to-r from-primary to-primary/70"
              style={{ width: `${hn * 100}%` }}
              aria-label="Home win probability"
            />
            {dn > 0 ? (
              <div
                className="h-full bg-gradient-to-r from-white/25 to-white/15"
                style={{ width: `${dn * 100}%` }}
                aria-label="Draw probability"
              />
            ) : null}
            <div
              className="h-full bg-gradient-to-r from-sidebar-accent/85 to-sidebar-accent/55"
              style={{ width: `${an * 100}%` }}
              aria-label="Away win probability"
            />
          </div>
        </div>

        <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
          <div className="rounded-xl border border-border/60 bg-white/3 px-2 py-1.5">
            <div className="text-muted-foreground">Home</div>
            <div className="font-semibold text-foreground">{pct(h)}</div>
          </div>
          <div className={cn("rounded-xl border px-2 py-1.5", dn > 0 ? "border-border/60 bg-white/3" : "border-border/40 bg-white/2 opacity-70")}>
            <div className="text-muted-foreground">Draw</div>
            <div className="font-semibold text-foreground">{dn > 0 ? pct(d) : "—"}</div>
          </div>
          <div className="rounded-xl border border-border/60 bg-white/3 px-2 py-1.5">
            <div className="text-muted-foreground">Away</div>
            <div className="font-semibold text-foreground">{pct(a)}</div>
          </div>
        </div>
      </div>
    </div>
  );
}
