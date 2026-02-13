import { z } from "zod";
import {
  insertGameSchema,
  insertPredictionSchema,
  type Game,
  type Prediction,
  type SeedStatusResponse,
} from "./schema";

export const errorSchemas = {
  validation: z.object({
    message: z.string(),
    field: z.string().optional(),
  }),
  notFound: z.object({
    message: z.string(),
  }),
  internal: z.object({
    message: z.string(),
  }),
};

export const api = {
  engine: {
    gamesWithPredictions: {
      method: "GET" as const,
      path: "/api/engine/games" as const,
      input: z
        .object({
          leagueId: z.coerce.number().optional(),
          status: z.string().optional(),
          from: z.string().optional(),
          to: z.string().optional(),
        })
        .optional(),
      responses: {
        200: z.array(
          z.object({
            id: z.number(),
            leagueId: z.number(),
            startTime: z.string(),
            status: z.string(),
            homeScore: z.number().nullable(),
            awayScore: z.number().nullable(),
            homeTeam: z.object({
              id: z.number(),
              name: z.string(),
              shortName: z.string(),
              slug: z.string(),
            }),
            awayTeam: z.object({
              id: z.number(),
              name: z.string(),
              shortName: z.string(),
              slug: z.string(),
            }),
            latestPrediction: z
              .object({
                id: z.number(),
                createdAt: z.string(),
                homeWinProb: z.number(),
                awayWinProb: z.number(),
                drawProb: z.number().nullable(),
                recommendedPick: z.string().nullable(),
                isFinal: z.boolean(),
                winner: z.string().nullable(),
              })
              .optional(),
          }),
        ),
      },
    },
  },
  games: {
    list: {
      method: "GET" as const,
      path: "/api/games" as const,
      input: z
        .object({
          leagueId: z.coerce.number().optional(),
          status: z.string().optional(),
        })
        .optional(),
      responses: {
        200: z.array(z.custom<Game>()),
      },
    },
    get: {
      method: "GET" as const,
      path: "/api/games/:id" as const,
      responses: {
        200: z.custom<Game>(),
        404: errorSchemas.notFound,
      },
    },
    create: {
      method: "POST" as const,
      path: "/api/games" as const,
      input: insertGameSchema.extend({
        leagueId: z.coerce.number(),
        homeTeamId: z.coerce.number(),
        awayTeamId: z.coerce.number(),
        startTime: z.coerce.date(),
      }),
      responses: {
        201: z.custom<Game>(),
        400: errorSchemas.validation,
      },
    },
    update: {
      method: "PUT" as const,
      path: "/api/games/:id" as const,
      input: insertGameSchema.partial().extend({
        leagueId: z.coerce.number().optional(),
        homeTeamId: z.coerce.number().optional(),
        awayTeamId: z.coerce.number().optional(),
        startTime: z.coerce.date().optional(),
        homeScore: z.coerce.number().optional(),
        awayScore: z.coerce.number().optional(),
        status: z.string().optional(),
      }),
      responses: {
        200: z.custom<Game>(),
        400: errorSchemas.validation,
        404: errorSchemas.notFound,
      },
    },
    delete: {
      method: "DELETE" as const,
      path: "/api/games/:id" as const,
      responses: {
        204: z.void(),
        404: errorSchemas.notFound,
      },
    },
  },
  predictions: {
    listForGame: {
      method: "GET" as const,
      path: "/api/games/:id/predictions" as const,
      responses: {
        200: z.array(z.custom<Prediction>()),
        404: errorSchemas.notFound,
      },
    },
    createForGame: {
      method: "POST" as const,
      path: "/api/games/:id/predictions" as const,
      input: insertPredictionSchema
        .pick({
          gameId: true,
        })
        .partial()
        .optional(),
      responses: {
        201: z.custom<Prediction>(),
        404: errorSchemas.notFound,
      },
    },
  },
  admin: {
    seed: {
      method: "POST" as const,
      path: "/api/admin/seed" as const,
      input: z.object({}).optional(),
      responses: {
        200: z.custom<SeedStatusResponse>(),
      },
    },
  },
};

export function buildUrl(
  path: string,
  params?: Record<string, string | number>,
): string {
  let url = path;
  if (params) {
    Object.entries(params).forEach(([key, value]) => {
      if (url.includes(`:${key}`)) {
        url = url.replace(`:${key}`, String(value));
      }
    });
  }
  return url;
}

export type EngineGamesResponse = z.infer<
  typeof api.engine.gamesWithPredictions.responses[200]
>;
export type GamesListResponse = z.infer<typeof api.games.list.responses[200]>;
export type GameResponse = z.infer<typeof api.games.get.responses[200]>;
export type CreateGameInput = z.infer<typeof api.games.create.input>;
export type UpdateGameInput = z.infer<typeof api.games.update.input>;
export type PredictionsListResponse = z.infer<
  typeof api.predictions.listForGame.responses[200]
>;
export type CreatePredictionInput = z.infer<
  typeof api.predictions.createForGame.input
>;
export type SeedStatus = z.infer<typeof api.admin.seed.responses[200]>;
export type ValidationError = z.infer<typeof errorSchemas.validation>;
export type NotFoundError = z.infer<typeof errorSchemas.notFound>;
export type InternalError = z.infer<typeof errorSchemas.internal>;
