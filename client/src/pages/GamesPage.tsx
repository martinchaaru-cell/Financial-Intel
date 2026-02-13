import * as React from "react";
import AppShell from "@/components/AppShell";
import { useGames, useCreateGame, useUpdateGame, useDeleteGame } from "@/hooks/use-games";
import { usePredictionsForGame } from "@/hooks/use-predictions";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { GameFormDialog } from "@/components/GameFormDialog";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ScoreUpdateCard } from "@/components/ScoreUpdateCard";
import { ProbabilityBar } from "@/components/ProbabilityBar";
import { cn } from "@/lib/utils";
import { Edit3, Plus, Search, Trash2 } from "lucide-react";
import type { Game } from "@shared/schema";

function fmtCompact(isoOrDate: any) {
  const d = typeof isoOrDate === "string" ? new Date(isoOrDate) : isoOrDate;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function GamesPage() {
  const { toast } = useToast();
  const [query, setQuery] = React.useState("");
  const [status, setStatus] = React.useState<string>("all");

  const games = useGames({ status: status === "all" ? undefined : status });
  const create = useCreateGame();
  const update = useUpdateGame();
  const del = useDeleteGame();

  const [selected, setSelected] = React.useState<Game | null>(null);
  const [createOpen, setCreateOpen] = React.useState(false);
  const [editOpen, setEditOpen] = React.useState(false);
  const [confirmOpen, setConfirmOpen] = React.useState(false);

  const filtered = (games.data ?? []).filter((g) => {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    // We don't have team names on /api/games; filter by id/league/status.
    return String(g.id).includes(q) || String(g.leagueId).includes(q) || (g.status ?? "").toLowerCase().includes(q);
  });

  const predictions = usePredictionsForGame(selected?.id ?? Number.NaN);

  return (
    <AppShell>
      <div className="anim-in">
        <header className="glass rounded-3xl p-6 sm:p-7">
          <div className="flex flex-col lg:flex-row lg:items-end lg:justify-between gap-6">
            <div className="min-w-0">
              <div className="inline-flex items-center gap-2 rounded-full border border-border/60 bg-white/5 px-3 py-1 text-xs font-semibold text-muted-foreground">
                <span className="inline-block h-1.5 w-1.5 rounded-full bg-sidebar-accent" />
                CRUD + scoring
              </div>
              <h1 className="mt-4 text-3xl sm:text-4xl leading-[1.05]">
                Manage games like a{" "}
                <span className="text-gradient">production desk</span>.
              </h1>
              <p className="mt-3 max-w-2xl text-base text-muted-foreground">
                Create schedule entries, edit metadata, and record final scores.
                Engine runs live in the Dashboard; predictions history lives per game.
              </p>
            </div>

            <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
              <Button
                data-testid="games-create"
                onClick={() => setCreateOpen(true)}
                className="rounded-xl font-semibold bg-gradient-to-r from-primary to-sidebar-accent text-primary-foreground shadow-lg shadow-primary/20 hover:shadow-xl hover:shadow-primary/25 hover:-translate-y-0.5 active:translate-y-0 transition-all duration-200"
              >
                <Plus className="h-4 w-4 mr-2" />
                Create game
              </Button>
            </div>
          </div>

          <Separator className="my-6 bg-border/60" />

          <div className="grid grid-cols-1 md:grid-cols-12 gap-3">
            <div className="md:col-span-8">
              <div className="relative">
                <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input
                  data-testid="games-search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search by game id, league id, or status…"
                  className="pl-10 focus-ring rounded-xl bg-background/30 border-border/70"
                />
              </div>
            </div>
            <div className="md:col-span-4">
              <Input
                data-testid="games-status-filter"
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                placeholder="all | scheduled | live | final"
                className="focus-ring rounded-xl bg-background/30 border-border/70"
              />
            </div>
          </div>
        </header>

        <div className="mt-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
          <section className="lg:col-span-7">
            <div className="glass rounded-3xl p-5 sm:p-6">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm text-muted-foreground">Games</div>
                  <div
                    className="mt-1 text-2xl font-bold"
                    style={{ fontFamily: "var(--font-serif)" }}
                  >
                    Schedule list
                  </div>
                </div>
                <Badge
                  variant="outline"
                  className="rounded-full border-border/60 bg-white/4 text-muted-foreground"
                  data-testid="games-count"
                >
                  {games.isLoading ? "Loading…" : `${filtered.length} shown`}
                </Badge>
              </div>

              <div className="mt-4 space-y-2">
                {games.isLoading ? (
                  <div className="space-y-2">
                    {Array.from({ length: 7 }).map((_, i) => (
                      <div
                        key={i}
                        className="rounded-2xl border border-border/60 bg-white/4 p-4"
                      >
                        <div className="flex items-center justify-between">
                          <Skeleton className="h-4 w-44 bg-white/10" />
                          <Skeleton className="h-8 w-28 bg-white/10" />
                        </div>
                        <Skeleton className="mt-3 h-3 w-64 bg-white/10" />
                      </div>
                    ))}
                  </div>
                ) : games.isError ? (
                  <div className="rounded-2xl border border-destructive/40 bg-destructive/10 p-4">
                    <div className="font-semibold">Couldn’t load games</div>
                    <div className="mt-1 text-sm text-muted-foreground">
                      {games.error instanceof Error ? games.error.message : "Unknown error"}
                    </div>
                    <div className="mt-3">
                      <Button
                        data-testid="games-retry"
                        onClick={() => games.refetch()}
                        className="rounded-xl bg-destructive text-destructive-foreground hover:bg-destructive/90"
                      >
                        Retry
                      </Button>
                    </div>
                  </div>
                ) : filtered.length === 0 ? (
                  <div className="rounded-2xl border border-border/60 bg-white/3 p-6">
                    <div
                      className="text-xl font-bold"
                      style={{ fontFamily: "var(--font-serif)" }}
                    >
                      No games match your search.
                    </div>
                    <p className="mt-2 text-muted-foreground">
                      Try a different query or create a new game.
                    </p>
                    <div className="mt-4">
                      <Button
                        data-testid="games-empty-create"
                        onClick={() => setCreateOpen(true)}
                        className="rounded-xl font-semibold bg-gradient-to-r from-primary to-sidebar-accent text-primary-foreground"
                      >
                        Create game
                      </Button>
                    </div>
                  </div>
                ) : (
                  filtered.map((g) => {
                    const isSelected = selected?.id === g.id;
                    return (
                      <button
                        key={g.id}
                        type="button"
                        data-testid={`games-row-${g.id}`}
                        onClick={() => setSelected(g)}
                        className={cn(
                          "w-full text-left rounded-2xl border p-4 transition-all duration-200 focus:outline-none focus-visible:ring-4 focus-visible:ring-ring/20",
                          isSelected
                            ? "border-primary/40 bg-primary/8 shadow-md shadow-black/60"
                            : "border-border/60 bg-white/3 hover:bg-white/4",
                        )}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <Badge
                                variant="outline"
                                className={cn(
                                  "rounded-full border-border/60 bg-white/4 text-muted-foreground",
                                  g.status === "final" && "border-primary/40 text-foreground",
                                )}
                                data-testid={`game-status-${g.id}`}
                              >
                                {g.status}
                              </Badge>
                              <div className="text-sm text-muted-foreground">
                                {fmtCompact(g.startTime as any)}
                              </div>
                            </div>
                            <div className="mt-2 font-semibold text-foreground">
                              Game #{g.id}{" "}
                              <span className="text-muted-foreground font-normal">
                                • League #{g.leagueId}
                              </span>
                            </div>
                            <div className="mt-1 text-sm text-muted-foreground">
                              Home team #{g.homeTeamId} vs Away team #{g.awayTeamId}
                            </div>
                          </div>

                          <div className="flex items-center gap-2">
                            <Button
                              data-testid={`games-edit-${g.id}`}
                              variant="outline"
                              size="icon"
                              onClick={(e) => {
                                e.stopPropagation();
                                setSelected(g);
                                setEditOpen(true);
                              }}
                              className="rounded-xl border-border/60 bg-white/5 hover:bg-white/8"
                            >
                              <Edit3 className="h-4 w-4" />
                            </Button>
                            <Button
                              data-testid={`games-delete-${g.id}`}
                              variant="outline"
                              size="icon"
                              onClick={(e) => {
                                e.stopPropagation();
                                setSelected(g);
                                setConfirmOpen(true);
                              }}
                              className="rounded-xl border-destructive/35 bg-destructive/10 text-destructive hover:bg-destructive/15"
                            >
                              <Trash2 className="h-4 w-4" />
                            </Button>
                          </div>
                        </div>
                      </button>
                    );
                  })
                )}
              </div>
            </div>
          </section>

          <aside className="lg:col-span-5">
            <div className="anim-in-delayed space-y-4">
              {selected ? (
                <>
                  <ScoreUpdateCard
                    gameId={selected.id}
                    status={selected.status}
                    homeScore={selected.homeScore ?? null}
                    awayScore={selected.awayScore ?? null}
                  />

                  <div className="glass rounded-3xl p-5 sm:p-6">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="text-sm text-muted-foreground">
                          Predictions history
                        </div>
                        <div
                          className="mt-1 text-xl font-bold"
                          style={{ fontFamily: "var(--font-serif)" }}
                        >
                          Game #{selected.id}
                        </div>
                      </div>
                      <Badge
                        variant="outline"
                        className="rounded-full border-border/60 bg-white/4 text-muted-foreground"
                        data-testid="predictions-count"
                      >
                        {predictions.isLoading
                          ? "Loading…"
                          : predictions.data
                            ? `${predictions.data.length} runs`
                            : "—"}
                      </Badge>
                    </div>

                    <div className="mt-4 space-y-3">
                      {predictions.isLoading ? (
                        <div className="space-y-2">
                          {Array.from({ length: 3 }).map((_, i) => (
                            <div
                              key={i}
                              className="rounded-2xl border border-border/60 bg-white/4 p-4"
                            >
                              <Skeleton className="h-4 w-40 bg-white/10" />
                              <Skeleton className="mt-3 h-3 w-full bg-white/10" />
                              <Skeleton className="mt-2 h-3 w-5/6 bg-white/10" />
                            </div>
                          ))}
                        </div>
                      ) : predictions.isError ? (
                        <div className="rounded-2xl border border-destructive/40 bg-destructive/10 p-4">
                          <div className="font-semibold">Couldn’t load predictions</div>
                          <div className="mt-1 text-sm text-muted-foreground">
                            {predictions.error instanceof Error ? predictions.error.message : "Unknown error"}
                          </div>
                        </div>
                      ) : !predictions.data || predictions.data.length === 0 ? (
                        <div className="rounded-2xl border border-border/60 bg-white/3 p-5">
                          <div className="font-semibold text-foreground">
                            No engine runs yet.
                          </div>
                          <div className="mt-1 text-sm text-muted-foreground">
                            Run the engine from the Dashboard to generate a line.
                          </div>
                        </div>
                      ) : (
                        predictions.data
                          .slice()
                          .sort((a, b) => new Date(b.createdAt as any).getTime() - new Date(a.createdAt as any).getTime())
                          .map((p) => (
                            <div
                              key={p.id}
                              className="rounded-2xl border border-border/60 bg-white/3 p-4"
                              data-testid={`prediction-row-${p.id}`}
                            >
                              <div className="flex items-center justify-between gap-3">
                                <div className="text-sm font-semibold text-foreground">
                                  Run #{p.id}
                                </div>
                                <div className="text-xs text-muted-foreground">
                                  {fmtCompact(p.createdAt as any)}
                                </div>
                              </div>

                              <div className="mt-3">
                                <ProbabilityBar
                                  homeLabel={`Home #${selected.homeTeamId}`}
                                  awayLabel={`Away #${selected.awayTeamId}`}
                                  homeProb={p.homeWinProb}
                                  awayProb={p.awayWinProb}
                                  drawProb={p.drawProb}
                                  recommendedPick={p.recommendedPick}
                                  testId={`prediction-prob-${p.id}`}
                                />
                              </div>
                            </div>
                          ))
                      )}
                    </div>
                  </div>
                </>
              ) : (
                <div className="rounded-3xl border border-border/60 bg-white/3 p-6">
                  <div
                    className="text-2xl font-bold"
                    style={{ fontFamily: "var(--font-serif)" }}
                  >
                    Select a game
                  </div>
                  <p className="mt-2 text-muted-foreground">
                    Click a game row to manage score/status and view its prediction history.
                  </p>
                </div>
              )}
            </div>
          </aside>
        </div>

        <GameFormDialog
          open={createOpen}
          onOpenChange={setCreateOpen}
          mode="create"
          isPending={create.isPending}
          onSubmit={(data) =>
            create.mutate(data as any, {
              onSuccess: () => {
                toast({ title: "Game created", description: "Schedule updated." });
                setCreateOpen(false);
              },
              onError: (e) =>
                toast({
                  title: "Create failed",
                  description: e instanceof Error ? e.message : "Unknown error",
                  variant: "destructive",
                }),
            })
          }
        />

        <GameFormDialog
          open={editOpen}
          onOpenChange={setEditOpen}
          mode="edit"
          isPending={update.isPending}
          initial={
            selected
              ? {
                  id: selected.id,
                  leagueId: selected.leagueId,
                  homeTeamId: selected.homeTeamId,
                  awayTeamId: selected.awayTeamId,
                  startTime: new Date(selected.startTime as any),
                  status: selected.status,
                }
              : undefined
          }
          onSubmit={(updates) => {
            if (!selected) return;
            update.mutate(
              { id: selected.id, updates: updates as any },
              {
                onSuccess: (updated) => {
                  toast({ title: "Game saved", description: "Changes applied." });
                  setSelected(updated as any);
                  setEditOpen(false);
                },
                onError: (e) =>
                  toast({
                    title: "Save failed",
                    description: e instanceof Error ? e.message : "Unknown error",
                    variant: "destructive",
                  }),
              },
            );
          }}
        />

        <ConfirmDialog
          open={confirmOpen}
          onOpenChange={setConfirmOpen}
          title="Delete game?"
          description={
            selected
              ? `This will permanently delete Game #${selected.id}.`
              : "This will permanently delete the selected game."
          }
          destructive
          confirmText={del.isPending ? "Deleting…" : "Delete"}
          onConfirm={() => {
            if (!selected) return;
            del.mutate(selected.id, {
              onSuccess: () => {
                toast({ title: "Deleted", description: "Game removed." });
                setConfirmOpen(false);
                setSelected(null);
              },
              onError: (e) =>
                toast({
                  title: "Delete failed",
                  description: e instanceof Error ? e.message : "Unknown error",
                  variant: "destructive",
                }),
            });
          }}
          testIdConfirm="confirm-delete-game"
        />
      </div>
    </AppShell>
  );
}
