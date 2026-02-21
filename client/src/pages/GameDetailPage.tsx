import * as React from "react";
import { useRoute } from "wouter";
import AppShell from "@/components/AppShell";
import { useEngineGames } from "@/hooks/use-engine";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ProbabilityBar } from "@/components/ProbabilityBar";
import { ShieldCheck, Globe, Calendar, Clock, ChevronLeft } from "lucide-react";
import { Link } from "wouter";

export default function GameDetailPage() {
  const [, params] = useRoute("/game/:id");
  const gameId = params?.id ? parseInt(params.id) : null;

  const engine = useEngineGames();
  const game = engine.data?.find((g) => g.id === gameId);

  if (engine.isLoading) {
    return (
      <AppShell>
        <Skeleton className="h-[600px] w-full rounded-3xl bg-white/5" />
      </AppShell>
    );
  }

  if (!game) {
    return (
      <AppShell>
        <div className="glass rounded-3xl p-12 text-center">
          <h3 className="text-xl font-semibold">Leg Not Found</h3>
          <Link href="/">
            <Button className="mt-4">Back to Dashboard</Button>
          </Link>
        </div>
      </AppShell>
    );
  }

  const pred = game.latestPrediction;

  const allChecksPassed = pred && (pred.checksPassed ?? 0) >= 42;

  return (
    <AppShell>
      <div className="anim-in">
        <Link href="/elite">
          <Button variant="ghost" className="mb-6 hover:bg-white/5 text-muted-foreground">
            <ChevronLeft className="h-4 w-4 mr-2" />
            Back to Elite Board
          </Button>
        </Link>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2 space-y-6">
            <div className="glass rounded-3xl p-8 border-primary/20 bg-primary/5">
              <div className="flex justify-between items-start mb-8">
                <div className="space-y-1">
                  <div className="flex items-center gap-2 text-sm text-primary font-medium uppercase tracking-wider">
                    <Globe className="h-4 w-4" />
                    {game.country} • {game.leagueName}
                  </div>
                  <div className="flex items-center gap-4 text-muted-foreground text-xs">
                    <div className="flex items-center gap-1">
                      <Calendar className="h-3 w-3" />
                      {new Date(game.startTime).toLocaleDateString()}
                    </div>
                    <div className="flex items-center gap-1">
                      <Clock className="h-3 w-3" />
                      {new Date(game.startTime).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                    </div>
                  </div>
                </div>
                {allChecksPassed && (
                  <Badge className="bg-primary text-primary-foreground font-bold px-4 py-1 animate-pulse">
                    ELITE PASS 42/42
                  </Badge>
                )}
              </div>

              <div className="flex items-center justify-between gap-8 mb-12">
                <div className="flex-1 text-center">
                  <div className="text-4xl font-bold mb-2">{game.homeTeam.name}</div>
                  <Badge variant="outline" className="text-xs">{game.homeTeam.shortName}</Badge>
                </div>
                <div className="text-2xl font-light text-muted-foreground italic">vs</div>
                <div className="flex-1 text-center">
                  <div className="text-4xl font-bold mb-2">{game.awayTeam.name}</div>
                  <Badge variant="outline" className="text-xs">{game.awayTeam.shortName}</Badge>
                </div>
              </div>

              {pred && (
                <div className="space-y-8">
                  <ProbabilityBar
                    homeLabel={game.homeTeam.shortName}
                    awayLabel={game.awayTeam.shortName}
                    homeProb={pred.homeWinProb}
                    awayProb={pred.awayWinProb}
                    drawProb={pred.drawProb}
                    recommendedPick={pred.recommendedPick}
                  />
                  
                  <div className="grid grid-cols-3 gap-4 text-center">
                    <div className="glass p-4 rounded-2xl">
                      <div className="text-xs text-muted-foreground uppercase mb-1">King of Hill</div>
                      <div className="text-2xl font-bold text-primary">{pred.kingOfHillScore}</div>
                    </div>
                    <div className="glass p-4 rounded-2xl">
                      <div className="text-xs text-muted-foreground uppercase mb-1">Checks</div>
                      <div className="text-2xl font-bold">{pred.checksPassed}/42</div>
                    </div>
                    <div className="glass p-4 rounded-2xl">
                      <div className="text-xs text-muted-foreground uppercase mb-1">Status</div>
                      <div className="text-2xl font-bold text-green-500 uppercase text-sm mt-1">Live Feed</div>
                    </div>
                  </div>
                </div>
              )}
            </div>

            {pred && (
              <div className="glass rounded-3xl p-8">
                <h3 className="text-xl font-bold mb-6 flex items-center gap-2">
                  <ShieldCheck className="h-5 w-5 text-primary" />
                  Full Forensic Audit Report
                </h3>
                <div className="bg-black/40 rounded-2xl p-6 font-mono text-sm leading-relaxed border border-white/5 max-h-[500px] overflow-y-auto custom-scrollbar">
                  <pre className="whitespace-pre-wrap text-muted-foreground/90">
                    {pred.forensicReport}
                  </pre>
                </div>
              </div>
            )}
          </div>

          <div className="space-y-6">
            <div className="glass rounded-3xl p-6 border-primary/20">
              <h4 className="font-bold mb-4 uppercase text-xs tracking-widest text-primary">Leg Metadata</h4>
              <div className="space-y-4 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Internal ID</span>
                  <span className="font-mono">#{game.id}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">League ID</span>
                  <span className="font-mono">#{game.leagueId}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Data Source</span>
                  <span className="text-green-500 font-medium">API-Sports (Live)</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Audit Version</span>
                  <span>Oracle Beast v48.16</span>
                </div>
              </div>
            </div>

            <div className="glass rounded-3xl p-6 bg-primary/5 border-primary/20">
              <h4 className="font-bold mb-2 uppercase text-xs tracking-widest">King of the Hill</h4>
              <p className="text-xs text-muted-foreground leading-relaxed">
                This leg is currently being ranked against all other legs playing today. 
                Ranking is determined by bilateral forensic scores, momentum variance, and clinical xG audits.
              </p>
            </div>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
