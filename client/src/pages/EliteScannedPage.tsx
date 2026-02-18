import * as React from "react";
import AppShell from "@/components/AppShell";
import { useEngineGames } from "@/hooks/use-engine";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ProbabilityBar } from "@/components/ProbabilityBar";
import { ShieldCheck, Info, RefreshCcw, Globe } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

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

export default function EliteScannedPage() {
  const engine = useEngineGames({
    status: "scheduled",
  });

  const eliteMatches = (engine.data ?? []).filter(
    (g) => g.latestPrediction && (g.latestPrediction.checksPassed ?? 0) >= 42
  );

  return (
    <AppShell>
      <div className="anim-in">
        <header className="glass rounded-3xl p-6 sm:p-7">
          <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-xl bg-primary/10 text-primary">
                <ShieldCheck className="h-6 w-6" />
              </div>
              <div>
                <h1 className="text-3xl font-bold tracking-tight">Elite Scanned</h1>
                <p className="text-muted-foreground">
                  Matches passing all 42 bilateral forensic checks.
                </p>
              </div>
            </div>
            <Button 
              onClick={() => engine.refetch()}
              variant="outline"
              className="rounded-xl border-primary/20 hover:bg-primary/5"
            >
              <RefreshCcw className="h-4 w-4 mr-2" />
              Scan Now
            </Button>
          </div>
        </header>

        <div className="mt-6">
          {engine.isLoading ? (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} className="h-48 rounded-3xl bg-white/5" />
              ))}
            </div>
          ) : eliteMatches.length === 0 ? (
            <div className="glass rounded-3xl p-12 text-center">
              <ShieldCheck className="h-12 w-12 mx-auto text-muted-foreground/30 mb-4" />
              <h3 className="text-xl font-semibold">No Elite Matches</h3>
              <p className="text-muted-foreground mt-2 max-w-md mx-auto">
                No matches currently meet the 100% bilateral check threshold (42/42). 
                Check back as the engine scans more fixtures.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {eliteMatches.map((g) => {
                const pred = g.latestPrediction!;
                return (
                  <div
                    key={g.id}
                    className="rounded-3xl border border-primary/20 bg-primary/5 p-5 hover:bg-primary/8 transition-colors relative overflow-hidden group"
                  >
                    <div className="absolute top-0 right-0 p-4 opacity-10 group-hover:opacity-20 transition-opacity">
                      <ShieldCheck className="h-24 w-24 text-primary" />
                    </div>

                    <div className="flex justify-between items-start mb-4">
                      <Badge className="bg-primary text-primary-foreground font-bold px-3">
                        ELITE PASS: 42/42
                      </Badge>
                      <div className="text-xs text-muted-foreground font-mono">
                        {fmtDate(g.startTime)}
                      </div>
                    </div>

                    <div className="text-xl font-bold mb-1">
                      {g.homeTeam.name} vs {g.awayTeam.name}
                    </div>
                    <div className="flex items-center gap-2 text-xs text-muted-foreground mb-4">
                      <Globe className="h-3 w-3" />
                      <span>League #{g.leagueId}</span>
                    </div>

                    <ProbabilityBar
                      homeLabel={g.homeTeam.shortName}
                      awayLabel={g.awayTeam.shortName}
                      homeProb={pred.homeWinProb}
                      awayProb={pred.awayWinProb}
                      drawProb={pred.drawProb}
                      recommendedPick={pred.recommendedPick}
                    />

                    <div className="mt-6 pt-4 border-t border-primary/10 flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-semibold text-primary uppercase tracking-wider">
                          Forensic Data
                        </span>
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button variant="ghost" size="icon" className="h-5 w-5 text-muted-foreground">
                                <Info className="h-3 w-3" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-xs p-3 font-mono text-[10px] leading-tight">
                              <pre className="whitespace-pre-wrap">
                                {pred.forensicReport}
                              </pre>
                            </TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      </div>
                      <Badge variant="outline" className="text-[10px] border-primary/30">
                        AUDITED BILATERAL
                      </Badge>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </AppShell>
  );
}
