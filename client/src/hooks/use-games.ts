import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, buildUrl, type CreateGameInput, type UpdateGameInput, type GamesListResponse, type GameResponse } from "@shared/routes";
import { z } from "zod";

function parseWithLogging<T>(schema: z.ZodSchema<T>, data: unknown, label: string): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    console.error(`[Zod] ${label} validation failed:`, result.error.format());
    throw result.error;
  }
  return result.data;
}

export function useGames(params?: { leagueId?: number; status?: string }) {
  return useQuery<GamesListResponse>({
    queryKey: [api.games.list.path, params ?? {}],
    queryFn: async () => {
      const search = new URLSearchParams();
      if (params?.leagueId !== undefined) search.set("leagueId", String(params.leagueId));
      if (params?.status) search.set("status", params.status);
      const url = search.toString() ? `${api.games.list.path}?${search.toString()}` : api.games.list.path;

      const res = await fetch(url, { credentials: "include" });
      if (!res.ok) throw new Error(`Failed to fetch games (${res.status})`);
      return parseWithLogging(api.games.list.responses[200], await res.json(), "games.list");
    },
  });
}

export function useGame(id: number) {
  return useQuery<GameResponse | null>({
    queryKey: [api.games.get.path, id],
    queryFn: async () => {
      const url = buildUrl(api.games.get.path, { id });
      const res = await fetch(url, { credentials: "include" });
      if (res.status === 404) return null;
      if (!res.ok) throw new Error(`Failed to fetch game (${res.status})`);
      return parseWithLogging(api.games.get.responses[200], await res.json(), "games.get");
    },
    enabled: Number.isFinite(id),
  });
}

export function useCreateGame() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateGameInput) => {
      const validated = api.games.create.input.parse(input);
      const res = await fetch(api.games.create.path, {
        method: api.games.create.method,
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(validated),
      });

      if (!res.ok) {
        if (res.status === 400) {
          const err = parseWithLogging(api.games.create.responses[400], await res.json(), "games.create.400");
          throw new Error(err.message);
        }
        throw new Error(`Failed to create game (${res.status})`);
      }
      return parseWithLogging(api.games.create.responses[201], await res.json(), "games.create.201");
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: [api.games.list.path] });
      await qc.invalidateQueries({ queryKey: [api.engine.gamesWithPredictions.path] });
    },
  });
}

export function useUpdateGame() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, updates }: { id: number; updates: UpdateGameInput }) => {
      const validated = api.games.update.input.parse(updates);
      const url = buildUrl(api.games.update.path, { id });
      const res = await fetch(url, {
        method: api.games.update.method,
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(validated),
      });

      if (!res.ok) {
        if (res.status === 400) {
          const err = parseWithLogging(api.games.update.responses[400], await res.json(), "games.update.400");
          throw new Error(err.message);
        }
        if (res.status === 404) {
          const err = parseWithLogging(api.games.update.responses[404], await res.json(), "games.update.404");
          throw new Error(err.message);
        }
        throw new Error(`Failed to update game (${res.status})`);
      }
      return parseWithLogging(api.games.update.responses[200], await res.json(), "games.update.200");
    },
    onSuccess: async (_data, variables) => {
      await qc.invalidateQueries({ queryKey: [api.games.list.path] });
      await qc.invalidateQueries({ queryKey: [api.engine.gamesWithPredictions.path] });
      await qc.invalidateQueries({ queryKey: [api.games.get.path, variables.id] });
    },
  });
}

export function useDeleteGame() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const url = buildUrl(api.games.delete.path, { id });
      const res = await fetch(url, { method: api.games.delete.method, credentials: "include" });

      if (res.status === 404) {
        const err = parseWithLogging(api.games.delete.responses[404], await res.json(), "games.delete.404");
        throw new Error(err.message);
      }
      if (!res.ok) throw new Error(`Failed to delete game (${res.status})`);
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: [api.games.list.path] });
      await qc.invalidateQueries({ queryKey: [api.engine.gamesWithPredictions.path] });
    },
  });
}
