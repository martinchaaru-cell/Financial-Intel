import { db } from "./db";
import {
  games,
  leagues,
  predictions,
  sports,
  teams,
  type CreateGameRequest,
  type CreatePredictionRequest,
  type Game,
  type GameWithTeams,
  type Prediction,
  type SeedStatusResponse,
  type UpdateGameRequest,
} from "@shared/schema";
import { and, desc, eq, gte, inArray, lte, sql } from "drizzle-orm";

export interface EngineGamesFilters {
  leagueId?: number;
  status?: string;
  from?: Date;
  to?: Date;
}

export interface IStorage {
  // Games
  listGames(filters?: { leagueId?: number; status?: string }): Promise<Game[]>;
  getGame(id: number): Promise<Game | undefined>;
  createGame(input: CreateGameRequest): Promise<Game>;
  updateGame(id: number, updates: UpdateGameRequest): Promise<Game | undefined>;
  deleteGame(id: number): Promise<boolean>;

  // Predictions
  listPredictionsForGame(gameId: number): Promise<Prediction[]>;
  createPrediction(input: CreatePredictionRequest): Promise<Prediction>;

  // Engine views
  listGamesWithTeamsAndLatestPrediction(
    filters?: EngineGamesFilters,
  ): Promise<GameWithTeams[]>;

  // Admin
  seed(): Promise<SeedStatusResponse>;
  seedIfEmpty(): Promise<SeedStatusResponse>;
}

function clamp01(n: number): number {
  if (Number.isNaN(n)) return 0;
  if (n < 0) return 0;
  if (n > 1) return 1;
  return n;
}

function round4(n: number): number {
  return Math.round(n * 10000) / 10000;
}

function hashStringToUnitInterval(s: string): number {
  // Stable pseudo-random in [0,1)
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  const unsigned = h >>> 0;
  return (unsigned % 100000) / 100000;
}

function computeEngineProbabilities(params: {
  homeTeamSlug: string;
  awayTeamSlug: string;
  leagueSlug: string;
  startTimeIso: string;
}): {
  homeWinProb: number;
  awayWinProb: number;
  drawProb: number | null;
  recommendedPick: string;
} {
  // MVP deterministic engine:
  // - Each team gets a stable "strength" derived from slug
  // - Home advantage bump
  // - Optional draw probability for soccer-style leagues (heuristic)

  const baseHome = 0.45 + (hashStringToUnitInterval(params.homeTeamSlug) - 0.5) * 0.16;
  const baseAway = 0.45 + (hashStringToUnitInterval(params.awayTeamSlug) - 0.5) * 0.16;
  const homeAdv = 0.04;

  let home = baseHome + homeAdv;
  let away = baseAway;

  // normalize
  const sum = home + away;
  home = home / sum;
  away = away / sum;

  const league = params.leagueSlug.toLowerCase();
  const isLikelyDrawLeague = league.includes("soccer") || league.includes("mls") || league.includes("epl");

  let draw: number | null = null;
  if (isLikelyDrawLeague) {
    const closeness = 1 - Math.abs(home - away); // 0..1
    draw = clamp01(0.12 + closeness * 0.18);
    // re-normalize home/away to (1-draw)
    const factor = 1 - draw;
    home = home * factor;
    away = away * factor;
  }

  const recommendedPick = draw !== null && draw > home && draw > away ? "DRAW" : home >= away ? "HOME" : "AWAY";

  return {
    homeWinProb: round4(clamp01(home)),
    awayWinProb: round4(clamp01(away)),
    drawProb: draw === null ? null : round4(clamp01(draw)),
    recommendedPick,
  };
}

export class DatabaseStorage implements IStorage {
  async listGames(filters?: { leagueId?: number; status?: string }): Promise<Game[]> {
    const where = [];
    if (filters?.leagueId !== undefined) {
      where.push(eq(games.leagueId, filters.leagueId));
    }
    if (filters?.status) {
      where.push(eq(games.status, filters.status));
    }

    const query = db
      .select()
      .from(games)
      .where(where.length ? and(...where) : undefined)
      .orderBy(desc(games.startTime));

    return await query;
  }

  async getGame(id: number): Promise<Game | undefined> {
    const [row] = await db.select().from(games).where(eq(games.id, id)).limit(1);
    return row;
  }

  async createGame(input: CreateGameRequest): Promise<Game> {
    const [created] = await db.insert(games).values(input).returning();
    return created;
  }

  async updateGame(id: number, updates: UpdateGameRequest): Promise<Game | undefined> {
    const [updated] = await db
      .update(games)
      .set(updates)
      .where(eq(games.id, id))
      .returning();
    return updated;
  }

  async deleteGame(id: number): Promise<boolean> {
    const [deleted] = await db.delete(games).where(eq(games.id, id)).returning();
    return Boolean(deleted);
  }

  async listPredictionsForGame(gameId: number): Promise<Prediction[]> {
    return await db
      .select()
      .from(predictions)
      .where(eq(predictions.gameId, gameId))
      .orderBy(desc(predictions.createdAt));
  }

  async createPrediction(input: CreatePredictionRequest): Promise<Prediction> {
    const gameId = input.gameId;

    const game = await this.getGame(gameId);
    if (!game) {
      throw new Error("GAME_NOT_FOUND");
    }

    const [homeTeam] = await db
      .select()
      .from(teams)
      .where(eq(teams.id, game.homeTeamId))
      .limit(1);
    const [awayTeam] = await db
      .select()
      .from(teams)
      .where(eq(teams.id, game.awayTeamId))
      .limit(1);
    const [league] = await db.select().from(leagues).where(eq(leagues.id, game.leagueId)).limit(1);

    if (!homeTeam || !awayTeam || !league) {
      throw new Error("ENGINE_CONTEXT_MISSING");
    }

    const engine = computeEngineProbabilities({
      homeTeamSlug: homeTeam.slug,
      awayTeamSlug: awayTeam.slug,
      leagueSlug: league.slug,
      startTimeIso: game.startTime.toISOString(),
    });

    const [created] = await db
      .insert(predictions)
      .values({
        gameId,
        homeWinProb: engine.homeWinProb,
        awayWinProb: engine.awayWinProb,
        drawProb: engine.drawProb,
        recommendedPick: engine.recommendedPick,
        isFinal: game.status === "final",
        winner:
          game.status === "final"
            ? game.homeScore === null || game.awayScore === null
              ? null
              : game.homeScore === game.awayScore
                ? "DRAW"
                : game.homeScore > game.awayScore
                  ? "HOME"
                  : "AWAY"
            : null,
      })
      .returning();

    return created;
  }

  async listGamesWithTeamsAndLatestPrediction(
    filters?: EngineGamesFilters,
  ): Promise<GameWithTeams[]> {
    const where = [];

    if (filters?.leagueId !== undefined) {
      where.push(eq(games.leagueId, filters.leagueId));
    }
    if (filters?.status) {
      where.push(eq(games.status, filters.status));
    }
    if (filters?.from) {
      where.push(gte(games.startTime, filters.from));
    }
    if (filters?.to) {
      where.push(lte(games.startTime, filters.to));
    }

    const rows = await db
      .select({
        game: games,
        home: {
          id: teams.id,
          name: teams.name,
          shortName: teams.shortName,
          slug: teams.slug,
        },
        away: {
          id: sql<number>`away_team.id`.as("id"),
          name: sql<string>`away_team.name`.as("name"),
          shortName: sql<string>`away_team.short_name`.as("shortName"),
          slug: sql<string>`away_team.slug`.as("slug"),
        },
        latestPred: {
          id: predictions.id,
          createdAt: predictions.createdAt,
          homeWinProb: predictions.homeWinProb,
          awayWinProb: predictions.awayWinProb,
          drawProb: predictions.drawProb,
          recommendedPick: predictions.recommendedPick,
          isFinal: predictions.isFinal,
          winner: predictions.winner,
        },
      })
      .from(games)
      .innerJoin(teams, eq(teams.id, games.homeTeamId))
      .innerJoin(sql`teams as away_team`, sql`away_team.id = ${games.awayTeamId}`)
      .leftJoin(
        predictions,
        and(
          eq(predictions.gameId, games.id),
          eq(
            predictions.id,
            sql<number>`(select p2.id from predictions p2 where p2.game_id = ${games.id} order by p2.created_at desc limit 1)`,
          ),
        ),
      )
      .where(where.length ? and(...where) : undefined)
      .orderBy(desc(games.startTime));

    return rows.map((r) => ({
      id: r.game.id,
      leagueId: r.game.leagueId,
      startTime: r.game.startTime.toISOString(),
      status: r.game.status,
      homeScore: r.game.homeScore ?? null,
      awayScore: r.game.awayScore ?? null,
      homeTeam: {
        id: r.home.id,
        name: r.home.name,
        shortName: r.home.shortName,
        slug: r.home.slug,
      },
      awayTeam: {
        id: r.away.id,
        name: r.away.name,
        shortName: r.away.shortName,
        slug: r.away.slug,
      },
      latestPrediction:
        r.latestPred?.id
          ? {
              id: r.latestPred.id,
              createdAt: r.latestPred.createdAt.toISOString(),
              homeWinProb: r.latestPred.homeWinProb,
              awayWinProb: r.latestPred.awayWinProb,
              drawProb: r.latestPred.drawProb ?? null,
              recommendedPick: r.latestPred.recommendedPick ?? null,
              isFinal: r.latestPred.isFinal,
              winner: r.latestPred.winner ?? null,
            }
          : undefined,
    }));
  }

  async seed(): Promise<SeedStatusResponse> {
    // Create baseline sports/leagues/teams/games/predictions.
    // Safe to run multiple times: checks for existing rows by slug.

    const existingSports = await db.select().from(sports);
    const existingLeagues = await db.select().from(leagues);
    const existingTeams = await db.select().from(teams);
    const existingGames = await db.select().from(games);
    const existingPreds = await db.select().from(predictions);

    const haveAny =
      existingSports.length +
        existingLeagues.length +
        existingTeams.length +
        existingGames.length +
        existingPreds.length >
      0;

    if (haveAny) {
      return {
        seeded: false,
        counts: {
          sports: existingSports.length,
          leagues: existingLeagues.length,
          teams: existingTeams.length,
          games: existingGames.length,
          predictions: existingPreds.length,
        },
      };
    }

    const [basketball] = await db
      .insert(sports)
      .values({ name: "Basketball", slug: "basketball" })
      .returning();
    const [soccer] = await db
      .insert(sports)
      .values({ name: "Soccer", slug: "soccer" })
      .returning();

    const [nba] = await db
      .insert(leagues)
      .values({ sportId: basketball.id, name: "NBA", slug: "nba" })
      .returning();
    const [mls] = await db
      .insert(leagues)
      .values({ sportId: soccer.id, name: "MLS", slug: "mls" })
      .returning();

    const teamDefs = [
      { leagueId: nba.id, name: "Boston Celtics", shortName: "BOS", slug: "bos" },
      { leagueId: nba.id, name: "Los Angeles Lakers", shortName: "LAL", slug: "lal" },
      { leagueId: nba.id, name: "Golden State Warriors", shortName: "GSW", slug: "gsw" },
      { leagueId: nba.id, name: "Milwaukee Bucks", shortName: "MIL", slug: "mil" },
      { leagueId: mls.id, name: "LA Galaxy", shortName: "LAG", slug: "la-galaxy" },
      { leagueId: mls.id, name: "Seattle Sounders", shortName: "SEA", slug: "seattle" },
      { leagueId: mls.id, name: "Inter Miami", shortName: "MIA", slug: "inter-miami" },
      { leagueId: mls.id, name: "Atlanta United", shortName: "ATL", slug: "atlanta" },
    ];

    const insertedTeams = await db.insert(teams).values(teamDefs).returning();
    const bySlug = new Map(insertedTeams.map((t) => [t.slug, t] as const));

    const now = new Date();
    const oneHour = 60 * 60 * 1000;

    const gameDefs: Array<CreateGameRequest> = [
      {
        leagueId: nba.id,
        startTime: new Date(now.getTime() + oneHour * 6),
        homeTeamId: bySlug.get("bos")!.id,
        awayTeamId: bySlug.get("lal")!.id,
        status: "scheduled",
      },
      {
        leagueId: nba.id,
        startTime: new Date(now.getTime() + oneHour * 30),
        homeTeamId: bySlug.get("gsw")!.id,
        awayTeamId: bySlug.get("mil")!.id,
        status: "scheduled",
      },
      {
        leagueId: mls.id,
        startTime: new Date(now.getTime() + oneHour * 10),
        homeTeamId: bySlug.get("inter-miami")!.id,
        awayTeamId: bySlug.get("seattle")!.id,
        status: "scheduled",
      },
      {
        leagueId: mls.id,
        startTime: new Date(now.getTime() - oneHour * 12),
        homeTeamId: bySlug.get("la-galaxy")!.id,
        awayTeamId: bySlug.get("atlanta")!.id,
        status: "final",
      },
    ];

    const insertedGames = await db.insert(games).values(gameDefs).returning();

    // Add scores for the final game (last one)
    const finalGame = insertedGames[insertedGames.length - 1];
    await db
      .update(games)
      .set({ homeScore: 2, awayScore: 1 })
      .where(eq(games.id, finalGame.id));

    // Create an initial prediction for each game
    for (const g of insertedGames) {
      await this.createPrediction({ gameId: g.id });
    }

    const [cSports] = await db.select({ c: sql<number>`count(*)` }).from(sports);
    const [cLeagues] = await db.select({ c: sql<number>`count(*)` }).from(leagues);
    const [cTeams] = await db.select({ c: sql<number>`count(*)` }).from(teams);
    const [cGames] = await db.select({ c: sql<number>`count(*)` }).from(games);
    const [cPreds] = await db.select({ c: sql<number>`count(*)` }).from(predictions);

    return {
      seeded: true,
      counts: {
        sports: Number(cSports.c),
        leagues: Number(cLeagues.c),
        teams: Number(cTeams.c),
        games: Number(cGames.c),
        predictions: Number(cPreds.c),
      },
    };
  }

  async seedIfEmpty(): Promise<SeedStatusResponse> {
    const [cGames] = await db.select({ c: sql<number>`count(*)` }).from(games);
    if (Number(cGames.c) > 0) {
      const [cSports] = await db.select({ c: sql<number>`count(*)` }).from(sports);
      const [cLeagues] = await db.select({ c: sql<number>`count(*)` }).from(leagues);
      const [cTeams] = await db.select({ c: sql<number>`count(*)` }).from(teams);
      const [cPreds] = await db.select({ c: sql<number>`count(*)` }).from(predictions);
      return {
        seeded: false,
        counts: {
          sports: Number(cSports.c),
          leagues: Number(cLeagues.c),
          teams: Number(cTeams.c),
          games: Number(cGames.c),
          predictions: Number(cPreds.c),
        },
      };
    }
    return await this.seed();
  }
}

export const storage = new DatabaseStorage();
