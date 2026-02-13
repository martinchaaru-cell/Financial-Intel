import AppShell from "@/components/AppShell";
import { Separator } from "@/components/ui/separator";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { Atom, Gauge, Layers, ShieldCheck } from "lucide-react";

function InfoCard({
  icon,
  title,
  children,
  tone,
  testId,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
  tone: "primary" | "accent" | "ember" | "muted";
  testId: string;
}) {
  const tones: Record<string, string> = {
    primary: "border-primary/30 bg-primary/10",
    accent: "border-sidebar-accent/30 bg-sidebar-accent/10",
    ember: "border-[hsl(var(--chart-2))]/30 bg-[hsl(var(--chart-2))]/10",
    muted: "border-border/60 bg-white/3",
  };

  return (
    <Card
      data-testid={testId}
      className={cn(
        "rounded-3xl p-6 shadow-[0_30px_80px_-52px_rgba(0,0,0,0.85)]",
        tones[tone],
      )}
    >
      <div className="flex items-start gap-3">
        <div className="grid h-11 w-11 place-items-center rounded-2xl border border-border/60 bg-white/5">
          {icon}
        </div>
        <div className="min-w-0">
          <div
            className="text-xl font-bold"
            style={{ fontFamily: "var(--font-serif)" }}
          >
            {title}
          </div>
          <div className="mt-2 text-sm text-muted-foreground leading-relaxed">
            {children}
          </div>
        </div>
      </div>
    </Card>
  );
}

export default function AboutPage() {
  return (
    <AppShell>
      <div className="anim-in">
        <header className="glass rounded-3xl p-6 sm:p-7">
          <div className="flex flex-col lg:flex-row lg:items-end lg:justify-between gap-6">
            <div className="min-w-0">
              <Badge
                variant="outline"
                className="rounded-full border-border/60 bg-white/4 text-muted-foreground"
                data-testid="about-badge"
              >
                Methodology
              </Badge>
              <h1 className="mt-4 text-3xl sm:text-4xl leading-[1.05]">
                A deterministic engine with{" "}
                <span className="text-gradient">auditable output</span>.
              </h1>
              <p className="mt-3 max-w-2xl text-base text-muted-foreground">
                This MVP is intentionally simple: predictable inputs, stable
                outputs, and a clean surface for iteration. Over time, the engine
                can evolve into a calibrated model with historical backtesting.
              </p>
            </div>
          </div>

          <Separator className="my-6 bg-border/60" />

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div className="rounded-2xl border border-border/60 bg-white/3 p-4">
              <div className="text-xs font-semibold text-muted-foreground">
                Version
              </div>
              <div
                className="mt-1 text-lg font-bold"
                style={{ fontFamily: "var(--font-serif)" }}
                data-testid="about-version"
              >
                Engine v1
              </div>
            </div>
            <div className="rounded-2xl border border-border/60 bg-white/3 p-4">
              <div className="text-xs font-semibold text-muted-foreground">
                Output
              </div>
              <div
                className="mt-1 text-lg font-bold"
                style={{ fontFamily: "var(--font-serif)" }}
                data-testid="about-output"
              >
                Probabilities (0–1)
              </div>
            </div>
            <div className="rounded-2xl border border-border/60 bg-white/3 p-4">
              <div className="text-xs font-semibold text-muted-foreground">
                Pick
              </div>
              <div
                className="mt-1 text-lg font-bold"
                style={{ fontFamily: "var(--font-serif)" }}
                data-testid="about-pick"
              >
                Deterministic recommendation
              </div>
            </div>
          </div>
        </header>

        <div className="mt-6 grid grid-cols-1 lg:grid-cols-2 gap-6">
          <InfoCard
            testId="about-card-1"
            tone="primary"
            icon={<Gauge className="h-5 w-5 text-primary" />}
            title="How v1 thinks"
          >
            v1 is a deterministic scoring function that outputs normalized win
            probabilities. In its simplest form:{" "}
            <span className="text-foreground font-semibold">
              team strength + home advantage
            </span>{" "}
            → probability line.
          </InfoCard>

          <InfoCard
            testId="about-card-2"
            tone="accent"
            icon={<Layers className="h-5 w-5 text-sidebar-accent" />}
            title="Why deterministic"
          >
            Determinism makes the system debuggable. If the same game produces a
            different line without an input change, something is wrong. It’s the
            right foundation before introducing data-driven variance.
          </InfoCard>

          <InfoCard
            testId="about-card-3"
            tone="ember"
            icon={<Atom className="h-5 w-5 text-[hsl(var(--chart-2))]" />}
            title="Next iterations"
          >
            Add historical calibration, league priors, and feature ingestion:
            injuries, travel, rest, market lines, and rating systems. Then layer
            backtesting + proper scoring rules to evaluate improvements.
          </InfoCard>

          <InfoCard
            testId="about-card-4"
            tone="muted"
            icon={<ShieldCheck className="h-5 w-5 text-foreground/90" />}
            title="Disclaimer"
          >
            This project is for demonstration and educational purposes. It does
            not constitute betting advice and should not be used to make
            financial decisions.
          </InfoCard>
        </div>

        <div className="mt-6 glass rounded-3xl p-6 sm:p-7">
          <div className="text-sm text-muted-foreground">Workflow</div>
          <div
            className="mt-1 text-2xl font-bold"
            style={{ fontFamily: "var(--font-serif)" }}
          >
            From schedule → line → final
          </div>

          <ol className="mt-4 grid grid-cols-1 md:grid-cols-3 gap-3">
            {[
              {
                title: "Create / seed",
                body: "Add games (or seed) so the schedule is populated.",
              },
              {
                title: "Run engine",
                body: "Generate a prediction line on demand. Each run is stored.",
              },
              {
                title: "Finalize",
                body: "Record score + status. Use history to evaluate future models.",
              },
            ].map((s, i) => (
              <li
                key={s.title}
                className="rounded-2xl border border-border/60 bg-white/3 p-4"
                data-testid={`workflow-step-${i + 1}`}
              >
                <div className="text-xs font-semibold text-muted-foreground">
                  Step {i + 1}
                </div>
                <div className="mt-1 text-lg font-bold">{s.title}</div>
                <div className="mt-2 text-sm text-muted-foreground leading-relaxed">
                  {s.body}
                </div>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </AppShell>
  );
}
