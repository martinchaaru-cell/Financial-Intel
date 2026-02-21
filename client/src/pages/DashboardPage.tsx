import * as React from "react";
import AppShell from "@/components/AppShell";
import { useEngineGames } from "@/hooks/use-engine";
import { useCreatePredictionForGame } from "@/hooks/use-predictions";
import { useSeedDatabase } from "@/hooks/use-admin";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { ProbabilityBar } from "@/components/ProbabilityBar";
import { StatPill } from "@/components/StatPill";
import { cn } from "@/lib/utils";
import { Flame, Filter, RefreshCcw } from "lucide-react";

import { Link } from "wouter";

function fmtDate(iso: string) {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusTone(status: string) {
  if (status === "final") return "bg-primary/14 border-primary/30 text-foreground";
  if (status === "live") return "bg-[hsl(var(--chart-2))]/14 border-[hsl(var(--chart-2))]/30 text-foreground";
  return "bg-white/4 border-border/60 text-muted-foreground";
}

export default function DashboardPage() {
  const { toast } = useToast();
  const [leagueId, setLeagueId] = React.useState<number | "all">("all");
  const [status, setStatus] = React.useState<string>("all");
  const [from, setFrom] = React.useState<string>("");
  const [to, setTo] = React.useState<string>("");

  const engine = useEngineGames({
    leagueId: leagueId === "all" ? undefined : leagueId,
    status: status === "all" ? undefined : status,
    from: from || undefined,
    to: to || undefined,
  });

  const seed = useSeedDatabase();
  const run = useCreatePredictionForGame();

  const items = engine.data ?? [];

  React.useEffect(() => {
    if (!engine.isLoading && engine.data && engine.data.length === 0) {
      // Attempt seed once automatically to make MVP feel alive.
      seed.mutate(undefined, {
        onSuccess: (data) =>
          toast({
            title: "Database seeded",
            description: `Loaded sample sports data (games: ${data.counts.games}, predictions: ${data.counts.predictions}).`,
          }),
        onError: () => {
          // Silent: backend may not support seed yet; user can still operate UI.
          console.warn("Seed failed (non-fatal).");
        },
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engine.isLoading, engine.data]);

  return (
    <AppShell>
      <div className="anim-in">
        <header className="glass rounded-3xl p-6 sm:p-7">
          <div className="flex flex-col lg:flex-row lg:items-end lg:justify-between gap-6">
            <div className="min-w-0">
              <div className="inline-flex items-center gap-2 rounded-full border border-border/60 bg-white/5 px-3 py-1 text-xs font-semibold text-muted-foreground">
                <span className="inline-block h-1.5 w-1.5 rounded-full bg-primary" />
                Engine board
              </div>
              <h1 className="mt-4 text-3xl sm:text-4xl leading-[1.05]">
                Live probability lines,{" "}
                <span className="text-gradient">engine-first</span>.
              </h1>
              <p className="mt-3 max-w-2xl text-base text-muted-foreground">
                This dashboard shows scheduled games with the latest deterministic
                model output. Run the engine to generate a fresh probability line
                and a recommended pick.
              </p>
            </div>

            <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
              <Button
                data-testid="dashboard-refresh"
                variant="outline"
                onClick={() => engine.refetch()}
                className="rounded-xl border-border/60 bg-white/5 hover:bg-white/8"
              >
                <RefreshCcw className="h-4 w-4 mr-2" />
                Refresh
              </Button>
              <Button
                data-testid="dashboard-seed"
                onClick={() =>
                  seed.mutate(undefined, {
                    onSuccess: (data) =>
                      toast({
                        title: "Seed complete",
                        description: `Counts — sports: ${data.counts.sports}, leagues: ${data.counts.leagues}, teams: ${data.counts.teams}, games: ${data.counts.games}.`,
                      }),
                    onError: (e) =>
                      toast({
                        title: "Seed failed",
                        description: e instanceof Error ? e.message : "Unknown error",
                        variant: "destructive",
                      }),
                  })
                }
                disabled={seed.isPending}
                className="rounded-xl font-semibold bg-gradient-to-r from-primary to-sidebar-accent text-primary-foreground shadow-lg shadow-primary/20 hover:shadow-xl hover:shadow-primary/25 hover:-translate-y-0.5 active:translate-y-0 transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed disabled:transform-none"
              >
                <Flame className="h-4 w-4 mr-2" />
                {seed.isPending ? "Seeding…" : "Seed sample data"}
              </Button>
            </div>
          </div>

          <Separator className="my-6 bg-border/60" />

          <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
            <div className="md:col-span-3">
              <div className="flex items-center gap-2 text-xs font-semibold text-muted-foreground mb-2">
                <Filter className="h-4 w-4" />
                Filters
              </div>
              <Select
                value={String(leagueId)}
                onValueChange={(v) => setLeagueId(v === "all" ? "all" : Number(v))}
              >
                <SelectTrigger
                  data-testid="filter-league"
                  className="focus-ring rounded-xl bg-background/30 border-border/70"
                >
                  <SelectValue placeholder="League" />
                </SelectTrigger>
                <SelectContent className="border-border/60 bg-popover">
                  <SelectItem value="all">All leagues</SelectItem>
                  <SelectItem value="1">League #1</SelectItem>
                  <SelectItem value="2">League #2</SelectItem>
                  <SelectItem value="3">League #3</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="md:col-span-3">
              <div className="text-xs font-semibold text-muted-foreground mb-2">
                Status
              </div>
              <Select value={status} onValueChange={setStatus}>
                <SelectTrigger
                  data-testid="filter-status"
                  className="focus-ring rounded-xl bg-background/30 border-border/70"
                >
                  <SelectValue placeholder="Status" />
                </SelectTrigger>
                <SelectContent className="border-border/60 bg-popover">
                  <SelectItem value="all">All</SelectItem>
                  <SelectItem value="scheduled">scheduled</SelectItem>
                  <SelectItem value="live">live</SelectItem>
                  <SelectItem value="final">final</SelectItem>
                  <SelectItem value="canceled">canceled</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="md:col-span-3">
              <div className="text-xs font-semibold text-muted-foreground mb-2">
                From (ISO date)
              </div>
              <Input
                data-testid="filter-from"
                value={from}
                onChange={(e) => setFrom(e.target.value)}
                placeholder="2026-02-01"
                className="focus-ring rounded-xl bg-background/30 border-border/70"
              />
            </div>

            <div className="md:col-span-3">
              <div className="text-xs font-semibold text-muted-foreground mb-2">
                To (ISO date)
              </div>
              <Input
                data-testid="filter-to"
                value={to}
                onChange={(e) => setTo(e.target.value)}
                placeholder="2026-02-28"
                className="focus-ring rounded-xl bg-background/30 border-border/70"
              />
            </div>
          </div>
        </header>

        <div className="mt-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
          <section className="lg:col-span-8">
            <div className="glass rounded-3xl p-5 sm:p-6">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm text-muted-foreground">
                    Games & engine output
                  </div>
                  <div
                    className="mt-1 text-2xl font-bold"
                    style={{ fontFamily: "var(--font-serif)" }}
                  >
                    Probability board
                  </div>
                </div>
                <Badge
                  variant="outline"
                  className="rounded-full border-border/60 bg-white/4 text-muted-foreground"
                  data-testid="dashboard-count"
                >
                  {engine.isLoading ? "Loading…" : `${items.length} games`}
                </Badge>
              </div>

              <div className="mt-4 space-y-3">
                {engine.isLoading ? (
                  <div className="space-y-3">
                    {Array.from({ length: 5 }).map((_, i) => (
                      <div
                        key={i}
                        className="rounded-2xl border border-border/60 bg-white/4 p-4"
                      >
                        <div className="flex items-center justify-between">
                          <Skeleton className="h-4 w-56 bg-white/10" />
                          <Skeleton className="h-5 w-24 bg-white/10" />
                        </div>
                        <div className="mt-4 space-y-2">
                          <Skeleton className="h-3 w-full bg-white/10" />
                          <div className="grid grid-cols-3 gap-2">
                            <Skeleton className="h-10 bg-white/10" />
                            <Skeleton className="h-10 bg-white/10" />
                            <Skeleton className="h-10 bg-white/10" />
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : engine.isError ? (
                  <div className="rounded-2xl border border-destructive/40 bg-destructive/10 p-4">
                    <div className="font-semibold">Couldn’t load engine board</div>
                    <div className="mt-1 text-sm text-muted-foreground">
                      {engine.error instanceof Error ? engine.error.message : "Unknown error"}
                    </div>
                    <div className="mt-3">
                      <Button
                        data-testid="dashboard-retry"
                        onClick={() => engine.refetch()}
                        className="rounded-xl bg-destructive text-destructive-foreground hover:bg-destructive/90"
                      >
                        Retry
                      </Button>
                    </div>
                  </div>
                ) : items.length === 0 ? (
                  <div className="rounded-2xl border border-border/60 bg-white/3 p-6">
                    <div
                      className="text-xl font-bold"
                      style={{ fontFamily: "var(--font-serif)" }}
                    >
                      No games yet.
                    </div>
                    <p className="mt-2 text-muted-foreground">
                      Seed sample data or create a game from the Games page.
                    </p>
                    <div className="mt-4 flex flex-col sm:flex-row gap-2">
                      <Button
                        data-testid="empty-seed"
                        onClick={() => seed.mutate()}
                        disabled={seed.isPending}
                        className="rounded-xl font-semibold bg-gradient-to-r from-primary to-sidebar-accent text-primary-foreground"
                      >
                        {seed.isPending ? "Seeding…" : "Seed sample data"}
                      </Button>
                      <Button
                        data-testid="empty-refresh"
                        variant="outline"
                        onClick={() => engine.refetch()}
                        className="rounded-xl border-border/60 bg-white/5 hover:bg-white/8"
                      >
                        Refresh
                      </Button>
                    </div>
                  </div>
                ) : (
                  items.map((g) => {
                    const pred = g.latestPrediction;
                    const runPending = run.isPending;
                    return (
                      <div
                        key={g.id}
                        className="rounded-3xl border border-border/60 bg-white/3 p-4 sm:p-5 hover:bg-white/4 transition-colors"
                        data-testid={`game-card-${g.id}`}
                      >
                        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <Badge
                                variant="outline"
                                className={cn("rounded-full", statusTone(g.status))}
                                data-testid={`game-status-${g.id}`}
                              >
                                {g.status}
                              </Badge>
                              <div
                                className="text-sm text-muted-foreground"
                                data-testid={`game-start-${g.id}`}
                              >
                                {fmtDate(g.startTime)}
                              </div>
                            </div>
                            <div className="mt-2">
                              <div
                                className="text-lg font-bold"
                                style={{ fontFamily: "var(--font-serif)" }}
                                data-testid={`game-teams-${g.id}`}
                              >
                                {g.homeTeam.name}{" "}
                                <span className="text-muted-foreground font-semibold">
                                  vs
                                </span>{" "}
                                {g.awayTeam.name}
                              </div>
                              <div className="mt-1 text-sm text-muted-foreground">
                                League #{g.leagueId} • Game #{g.id}
                              </div>
                            </div>
                          </div>

                          <div className="flex flex-col sm:items-end gap-2">
                            <Button
                              data-testid={`run-engine-${g.id}`}
                              onClick={() =>
                                run.mutate(
                                  { gameId: g.id },
                                  {
                                    onSuccess: () =>
                                      toast({
                                        title: "Engine run complete",
                                        description:
                                          "A new prediction has been added to history.",
                                      }),
                                    onError: (e) =>
                                      toast({
                                        title: "Engine run failed",
                                        description:
                                          e instanceof Error ? e.message : "Unknown error",
                                        variant: "destructive",
                                      }),
                                  },
                                )
                              }
                              disabled={runPending}
                              className="rounded-xl font-semibold bg-gradient-to-r from-primary to-sidebar-accent text-primary-foreground shadow-lg shadow-primary/20 hover:shadow-xl hover:shadow-primary/25 hover:-translate-y-0.5 active:translate-y-0 transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed disabled:transform-none"
                            >
                              {runPending ? "Running…" : "Run engine"}
                            </Button>

                            <div className="text-xs text-muted-foreground">
                              {pred ? `Updated ${fmtDate(pred.createdAt)}` : "No prediction yet"}
                            </div>
                          </div>
                        </div>

                        <div className="mt-4">
                          <ProbabilityBar
                            testId={`probability-${g.id}`}
                            homeLabel={g.homeTeam.shortName}
                            awayLabel={g.awayTeam.shortName}
                            homeProb={pred?.homeWinProb}
                            awayProb={pred?.awayWinProb}
                            drawProb={pred?.drawProb}
                            recommendedPick={pred?.recommendedPick}
                          />
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          </section>

          <aside className="lg:col-span-4">
            <div className="anim-in-delayed space-y-4">
              <div className="glass rounded-3xl p-5 sm:p-6">
                <div className="text-sm text-muted-foreground">Snapshot</div>
                <div
                  className="mt-1 text-2xl font-bold"
                  style={{ fontFamily: "var(--font-serif)" }}
                >
                  Today’s surface
                </div>
                <p className="mt-2 text-sm text-muted-foreground leading-relaxed">
                  Track how many games have an engine line. For v1, the engine is
                  deterministic: same inputs → same probabilities.
                </p>

                <div className="mt-4 grid grid-cols-2 gap-3">
                  <StatPill
                    testId="stat-total"
                    label="Games"
                    value={engine.isLoading ? "…" : String(items.length)}
                    tone="muted"
                  />
                  <StatPill
                    testId="stat-lined"
                    label="With line"
                    value={
                      engine.isLoading
                        ? "…"
                        : String(items.filter((x) => x.latestPrediction).length)
                    }
                    tone="primary"
                  />
                  <StatPill
                    testId="stat-scheduled"
                    label="Scheduled"
                    value={
                      engine.isLoading
                        ? "…"
                        : String(items.filter((x) => x.status === "scheduled").length)
                    }
                    tone="accent"
                  />
                  <StatPill
                    testId="stat-live"
                    label="Live"
                    value={
                      engine.isLoading
                        ? "…"
                        : String(items.filter((x) => x.status === "live").length)
                    }
                    tone="ember"
                  />
                </div>
              </div>

              <div className="rounded-3xl border border-border/60 bg-white/3 p-5 sm:p-6">
                <div className="text-sm text-muted-foreground">Integrity</div>
                <div
                  className="mt-1 text-xl font-bold"
                  style={{ fontFamily: "var(--font-serif)" }}
                >
                  Model caveats
                </div>
                <ul className="mt-3 space-y-2 text-sm text-muted-foreground leading-relaxed">
                  <li className="flex gap-2">
                    <span className="mt-1.5 h-1.5 w-1.5 rounded-full bg-primary" />
                    Not betting advice. This is a demo engine.
                  </li>
                  <li className="flex gap-2">
                    <span className="mt-1.5 h-1.5 w-1.5 rounded-full bg-sidebar-accent" />
                    v1 doesn’t ingest injuries, travel, or market odds.
                  </li>
                  <li className="flex gap-2">
                    <span className="mt-1.5 h-1.5 w-1.5 rounded-full bg-[hsl(var(--chart-2))]" />
                    Deterministic output helps debugging and iteration.
                  </li>
                </ul>
              </div>
            </div>
          </aside>
        </div>
      </div>
    </AppShell>
  );
}
