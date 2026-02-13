import * as React from "react";
import { useUpdateGame } from "@/hooks/use-games";
import { useToast } from "@/hooks/use-toast";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export function ScoreUpdateCard({
  gameId,
  status,
  homeScore,
  awayScore,
}: {
  gameId: number;
  status: string;
  homeScore: number | null;
  awayScore: number | null;
}) {
  const { toast } = useToast();
  const update = useUpdateGame();

  const [hs, setHs] = React.useState<string>(homeScore?.toString() ?? "");
  const [as, setAs] = React.useState<string>(awayScore?.toString() ?? "");
  const [st, setSt] = React.useState<string>(status ?? "scheduled");

  React.useEffect(() => {
    setHs(homeScore?.toString() ?? "");
    setAs(awayScore?.toString() ?? "");
    setSt(status ?? "scheduled");
  }, [homeScore, awayScore, status]);

  const canSubmit =
    (hs.trim().length > 0 || as.trim().length > 0 || st !== status) &&
    !update.isPending;

  async function submit() {
    const updates: any = { status: st };
    if (hs.trim() !== "") updates.homeScore = Number(hs);
    if (as.trim() !== "") updates.awayScore = Number(as);

    update.mutate(
      { id: gameId, updates },
      {
        onSuccess: () =>
          toast({
            title: "Game updated",
            description: "Scores/status saved successfully.",
          }),
        onError: (e) =>
          toast({
            title: "Update failed",
            description: e instanceof Error ? e.message : "Unknown error",
            variant: "destructive",
          }),
      },
    );
  }

  return (
    <div className="rounded-3xl border border-border/60 bg-white/4 p-5 shadow-[0_30px_80px_-52px_rgba(0,0,0,0.85)]">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm text-muted-foreground">Score & status</div>
          <div
            className="text-xl font-bold"
            style={{ fontFamily: "var(--font-serif)" }}
          >
            Finalize the outcome
          </div>
        </div>
        <Badge
          variant="outline"
          className={cn(
            "rounded-full border-border/60 bg-white/4 text-muted-foreground",
            st === "final" && "border-primary/40 text-foreground",
          )}
          data-testid="score-status-badge"
        >
          {st}
        </Badge>
      </div>

      <div className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="space-y-2">
          <Label>Home score</Label>
          <Input
            data-testid="score-home"
            value={hs}
            onChange={(e) => setHs(e.target.value)}
            type="number"
            inputMode="numeric"
            className="focus-ring rounded-xl bg-background/30 border-border/70"
          />
        </div>
        <div className="space-y-2">
          <Label>Away score</Label>
          <Input
            data-testid="score-away"
            value={as}
            onChange={(e) => setAs(e.target.value)}
            type="number"
            inputMode="numeric"
            className="focus-ring rounded-xl bg-background/30 border-border/70"
          />
        </div>
        <div className="space-y-2">
          <Label>Status</Label>
          <Input
            data-testid="score-status"
            value={st}
            onChange={(e) => setSt(e.target.value)}
            placeholder="scheduled | live | final"
            className="focus-ring rounded-xl bg-background/30 border-border/70"
          />
        </div>
      </div>

      <div className="mt-4 flex flex-col sm:flex-row gap-2">
        <Button
          onClick={() => {
            setHs(homeScore?.toString() ?? "");
            setAs(awayScore?.toString() ?? "");
            setSt(status ?? "scheduled");
          }}
          variant="outline"
          data-testid="score-reset"
          className="rounded-xl border-border/60 bg-white/5 hover:bg-white/8"
        >
          Reset
        </Button>
        <Button
          onClick={submit}
          disabled={!canSubmit}
          data-testid="score-save"
          className="rounded-xl font-semibold bg-gradient-to-r from-primary to-sidebar-accent text-primary-foreground shadow-lg shadow-primary/20 hover:shadow-xl hover:shadow-primary/25 hover:-translate-y-0.5 active:translate-y-0 transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed disabled:transform-none"
        >
          {update.isPending ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
  );
}
