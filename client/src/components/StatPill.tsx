import { cn } from "@/lib/utils";

export function StatPill({
  label,
  value,
  tone = "primary",
  testId,
}: {
  label: string;
  value: string;
  tone?: "primary" | "accent" | "muted" | "ember";
  testId?: string;
}) {
  const tones: Record<string, string> = {
    primary:
      "bg-primary/14 border-primary/30 text-foreground shadow-[0_12px_34px_-22px_rgba(0,0,0,0.85)]",
    accent:
      "bg-sidebar-accent/14 border-sidebar-accent/30 text-foreground shadow-[0_12px_34px_-22px_rgba(0,0,0,0.85)]",
    ember:
      "bg-[hsl(var(--chart-2))]/14 border-[hsl(var(--chart-2))]/30 text-foreground shadow-[0_12px_34px_-22px_rgba(0,0,0,0.85)]",
    muted: "bg-white/4 border-border/60 text-muted-foreground",
  };

  return (
    <div
      data-testid={testId}
      className={cn(
        "rounded-2xl border px-3 py-2 backdrop-blur-sm",
        tones[tone],
      )}
    >
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground/90">
        {label}
      </div>
      <div className="mt-0.5 text-sm font-semibold text-foreground">
        {value}
      </div>
    </div>
  );
}
