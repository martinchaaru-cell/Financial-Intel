import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@shared/routes";
import { z } from "zod";

function parseWithLogging<T>(schema: z.ZodSchema<T>, data: unknown, label: string): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    console.error(`[Zod] ${label} validation failed:`, result.error.format());
    throw result.error;
  }
  return result.data;
}

export function useSeedDatabase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const res = await fetch(api.admin.seed.path, {
        method: api.admin.seed.method,
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({}),
      });

      if (!res.ok) throw new Error(`Failed to seed (${res.status})`);
      return parseWithLogging(api.admin.seed.responses[200], await res.json(), "admin.seed.200");
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: [api.engine.gamesWithPredictions.path] });
      await qc.invalidateQueries({ queryKey: [api.games.list.path] });
    },
  });
}
