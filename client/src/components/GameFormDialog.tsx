import * as React from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api } from "@shared/routes";
import type { CreateGameInput, UpdateGameInput } from "@shared/routes";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const formSchema = api.games.create.input.extend({
  leagueId: z.coerce.number(),
  homeTeamId: z.coerce.number(),
  awayTeamId: z.coerce.number(),
  startTime: z.coerce.date(),
});

type FormValues = z.infer<typeof formSchema>;

function toLocalDateTimeValue(d: Date) {
  const pad = (n: number) => String(n).padStart(2, "0");
  const yyyy = d.getFullYear();
  const mm = pad(d.getMonth() + 1);
  const dd = pad(d.getDate());
  const hh = pad(d.getHours());
  const mi = pad(d.getMinutes());
  return `${yyyy}-${mm}-${dd}T${hh}:${mi}`;
}

export function GameFormDialog({
  open,
  onOpenChange,
  mode,
  initial,
  onSubmit,
  isPending,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  mode: "create" | "edit";
  initial?: Partial<FormValues> & { id?: number };
  onSubmit: (data: CreateGameInput | UpdateGameInput) => void;
  isPending?: boolean;
}) {
  const form = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      leagueId: initial?.leagueId ?? 1,
      homeTeamId: initial?.homeTeamId ?? 1,
      awayTeamId: initial?.awayTeamId ?? 2,
      startTime: initial?.startTime ?? new Date(),
      status: initial?.status ?? "scheduled",
    } as FormValues,
  });

  React.useEffect(() => {
    if (!open) return;
    form.reset({
      leagueId: initial?.leagueId ?? 1,
      homeTeamId: initial?.homeTeamId ?? 1,
      awayTeamId: initial?.awayTeamId ?? 2,
      startTime: initial?.startTime ?? new Date(),
      status: (initial?.status ?? "scheduled") as any,
    } as FormValues);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const title = mode === "create" ? "Create game" : "Edit game";
  const description =
    mode === "create"
      ? "Add a new matchup to the schedule. (Teams/leagues are seeded in MVP.)"
      : "Adjust schedule metadata. Use the Score panel to set finals.";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="border-border/60 bg-popover text-popover-foreground sm:max-w-xl">
        <DialogHeader>
          <DialogTitle style={{ fontFamily: "var(--font-serif)" }}>
            {title}
          </DialogTitle>
          <DialogDescription className="text-muted-foreground">
            {description}
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={form.handleSubmit((vals) => {
            const payload: CreateGameInput = {
              leagueId: vals.leagueId,
              homeTeamId: vals.homeTeamId,
              awayTeamId: vals.awayTeamId,
              startTime: vals.startTime,
              status: vals.status,
            };
            onSubmit(payload);
          })}
          className="space-y-4"
        >
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="leagueId">League ID</Label>
              <Input
                id="leagueId"
                data-testid="game-form-leagueId"
                type="number"
                inputMode="numeric"
                className="focus-ring rounded-xl bg-background/30 border-border/70"
                {...form.register("leagueId", { valueAsNumber: true })}
              />
              {form.formState.errors.leagueId ? (
                <p className="text-xs text-destructive">
                  {form.formState.errors.leagueId.message}
                </p>
              ) : null}
            </div>

            <div className="space-y-2">
              <Label>Status</Label>
              <Select
                value={form.watch("status") ?? "scheduled"}
                onValueChange={(v) => form.setValue("status", v as any, { shouldDirty: true })}
              >
                <SelectTrigger
                  data-testid="game-form-status"
                  className="focus-ring rounded-xl bg-background/30 border-border/70"
                >
                  <SelectValue placeholder="Select status" />
                </SelectTrigger>
                <SelectContent className="border-border/60 bg-popover">
                  <SelectItem value="scheduled">scheduled</SelectItem>
                  <SelectItem value="live">live</SelectItem>
                  <SelectItem value="final">final</SelectItem>
                  <SelectItem value="canceled">canceled</SelectItem>
                </SelectContent>
              </Select>
              {form.formState.errors.status ? (
                <p className="text-xs text-destructive">
                  {form.formState.errors.status.message}
                </p>
              ) : null}
            </div>

            <div className="space-y-2">
              <Label htmlFor="homeTeamId">Home Team ID</Label>
              <Input
                id="homeTeamId"
                data-testid="game-form-homeTeamId"
                type="number"
                inputMode="numeric"
                className="focus-ring rounded-xl bg-background/30 border-border/70"
                {...form.register("homeTeamId", { valueAsNumber: true })}
              />
              {form.formState.errors.homeTeamId ? (
                <p className="text-xs text-destructive">
                  {form.formState.errors.homeTeamId.message}
                </p>
              ) : null}
            </div>

            <div className="space-y-2">
              <Label htmlFor="awayTeamId">Away Team ID</Label>
              <Input
                id="awayTeamId"
                data-testid="game-form-awayTeamId"
                type="number"
                inputMode="numeric"
                className="focus-ring rounded-xl bg-background/30 border-border/70"
                {...form.register("awayTeamId", { valueAsNumber: true })}
              />
              {form.formState.errors.awayTeamId ? (
                <p className="text-xs text-destructive">
                  {form.formState.errors.awayTeamId.message}
                </p>
              ) : null}
            </div>

            <div className="space-y-2 sm:col-span-2">
              <Label htmlFor="startTime">Start time</Label>
              <Input
                id="startTime"
                data-testid="game-form-startTime"
                type="datetime-local"
                className="focus-ring rounded-xl bg-background/30 border-border/70"
                value={toLocalDateTimeValue(form.watch("startTime") ?? new Date())}
                onChange={(e) => {
                  const v = e.target.value;
                  const d = v ? new Date(v) : new Date();
                  form.setValue("startTime", d, { shouldDirty: true });
                }}
              />
              {form.formState.errors.startTime ? (
                <p className="text-xs text-destructive">
                  {form.formState.errors.startTime.message}
                </p>
              ) : null}
            </div>
          </div>

          <DialogFooter className="gap-2 sm:gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              data-testid="game-form-cancel"
              className="rounded-xl border-border/60 bg-white/5 hover:bg-white/8"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              data-testid="game-form-submit"
              disabled={isPending}
              className="rounded-xl font-semibold bg-gradient-to-r from-primary to-sidebar-accent text-primary-foreground shadow-lg shadow-primary/20 hover:shadow-xl hover:shadow-primary/25 hover:-translate-y-0.5 active:translate-y-0 transition-all duration-200"
            >
              {isPending ? "Saving…" : mode === "create" ? "Create game" : "Save changes"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
