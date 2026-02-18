import { useQuery } from "@tanstack/react-query";
import { api } from "@shared/routes";
import type { EngineGamesResponse } from "@shared/routes";
import { z } from "zod";

function parseWithLogging<T>(schema: z.ZodSchema<T>, data: unknown, label: string): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    console.error(`[Zod] ${label} validation failed:`, result.error.format());
    throw result.error;
  }
  return result.data;
}

export function useEngineGames(params?: {
  leagueId?: number;
  status?: string;
  from?: string;
  to?: string;
}) {
  return useQuery<EngineGamesResponse>({
    queryKey: [api.engine.gamesWithPredictions.path, params ?? {}],
    queryFn: async () => {
      const validated = api.engine.gamesWithPredictions.input?.safeParse(params);
      if (validated && !validated.success) {
        console.error("[Zod] engine.gamesWithPredictions input invalid:", validated.error.format());
      }
      const search = new URLSearchParams();
      const p = params ?? {};
      if (p.leagueId !== undefined) search.set("leagueId", String(p.leagueId));
      if (p.status) search.set("status", p.status);
      if (p.from) search.set("from", p.from);
      if (p.to) search.set("to", p.to);

      const url =
        search.toString().length > 0
          ? `${api.engine.gamesWithPredictions.path}?${search.toString()}`
          : api.engine.gamesWithPredictions.path;

      const res = await fetch(url, { credentials: "include" });
      if (!res.ok) throw new Error(`Failed to fetch engine games (${res.status})`);
      const json = await res.json();
      console.log("[Elite] Matches fetched:", json.length);
      return parseWithLogging(api.engine.gamesWithPredictions.responses[200], json, "engine.gamesWithPredictions");
    },
  });
}
