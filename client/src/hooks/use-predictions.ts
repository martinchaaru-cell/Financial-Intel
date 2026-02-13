import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, buildUrl, type PredictionsListResponse } from "@shared/routes";
import { z } from "zod";

function parseWithLogging<T>(schema: z.ZodSchema<T>, data: unknown, label: string): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    console.error(`[Zod] ${label} validation failed:`, result.error.format());
    throw result.error;
  }
  return result.data;
}

export function usePredictionsForGame(gameId: number) {
  return useQuery<PredictionsListResponse | null>({
    queryKey: [api.predictions.listForGame.path, gameId],
    queryFn: async () => {
      const url = buildUrl(api.predictions.listForGame.path, { id: gameId });
      const res = await fetch(url, { credentials: "include" });
      if (res.status === 404) return null;
      if (!res.ok) throw new Error(`Failed to fetch predictions (${res.status})`);
      return parseWithLogging(api.predictions.listForGame.responses[200], await res.json(), "predictions.listForGame");
    },
    enabled: Number.isFinite(gameId),
  });
}

export function useCreatePredictionForGame() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ gameId }: { gameId: number }) => {
      const url = buildUrl(api.predictions.createForGame.path, { id: gameId });

      // input is optional/partial; backend can infer gameId from :id
      const validated = api.predictions.createForGame.input?.parse({ gameId });

      const res = await fetch(url, {
        method: api.predictions.createForGame.method,
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(validated ?? {}),
      });

      if (!res.ok) {
        if (res.status === 404) {
          const err = parseWithLogging(api.predictions.createForGame.responses[404], await res.json(), "predictions.createForGame.404");
          throw new Error(err.message);
        }
        throw new Error(`Failed to run engine (${res.status})`);
      }
      return parseWithLogging(api.predictions.createForGame.responses[201], await res.json(), "predictions.createForGame.201");
    },
    onSuccess: async (_data, vars) => {
      await qc.invalidateQueries({ queryKey: [api.predictions.listForGame.path, vars.gameId] });
      await qc.invalidateQueries({ queryKey: [api.engine.gamesWithPredictions.path] });
    },
  });
}
