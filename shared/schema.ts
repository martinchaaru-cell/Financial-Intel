import { sql } from "drizzle-orm";
import {
  boolean,
  doublePrecision,
  integer,
  pgTable,
  serial,
  text,
  timestamp,
  varchar,
} from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod";

// =====================================================
// USERS
// =====================================================
export const users = pgTable("users", {
  id: varchar("id").primaryKey().default(sql`gen_random_uuid()`),
  username: text("username").notNull().unique(),
  password: text("password").notNull(),
});

export const insertUserSchema = createInsertSchema(users).pick({
  username: true,
  password: true,
});

export type InsertUser = z.infer<typeof insertUserSchema>;
export type User = typeof users.$inferSelect;

// =====================================================
// SPORTS ENGINE: GAMES + PREDICTIONS
// MVP:
// - Create/read games
// - Create predictions for games
// - Show simple "engine" probabilities (v1: deterministic)
// =====================================================

export const sports = pgTable("sports", {
  id: serial("id").primaryKey(),
  name: text("name").notNull(),
  slug: text("slug").notNull().unique(),
});

export type Sport = typeof sports.$inferSelect;

export const leagues = pgTable("leagues", {
  id: serial("id").primaryKey(),
  sportId: integer("sport_id").notNull(),
  name: text("name").notNull(),
  slug: text("slug").notNull().unique(),
});

export type League = typeof leagues.$inferSelect;

export const teams = pgTable("teams", {
  id: serial("id").primaryKey(),
  leagueId: integer("league_id").notNull(),
  name: text("name").notNull(),
  shortName: text("short_name").notNull(),
  slug: text("slug").notNull().unique(),
});

export type Team = typeof teams.$inferSelect;

export const games = pgTable("games", {
  id: serial("id").primaryKey(),
  leagueId: integer("league_id").notNull(),
  startTime: timestamp("start_time").notNull(),
  homeTeamId: integer("home_team_id").notNull(),
  awayTeamId: integer("away_team_id").notNull(),
  status: text("status").notNull().default("scheduled"),
  homeScore: integer("home_score"),
  awayScore: integer("away_score"),
});

export type Game = typeof games.$inferSelect;

export const predictions = pgTable("predictions", {
  id: serial("id").primaryKey(),
  gameId: integer("game_id").notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),

  // Engine output (probabilities 0..1)
  homeWinProb: doublePrecision("home_win_prob").notNull(),
  awayWinProb: doublePrecision("away_win_prob").notNull(),
  drawProb: doublePrecision("draw_prob"),

  // Optional: derived recommendation
  recommendedPick: text("recommended_pick"),

  // Forensic report (42-check bilateral report)
  forensicReport: text("forensic_report"),
  checksPassed: integer("checks_passed").default(0),

  // Actual outcomes for grading
  isFinal: boolean("is_final").notNull().default(false),
  winner: text("winner"),
});

export type Prediction = typeof predictions.$inferSelect;

// =====================================================
// INSERT SCHEMAS
// =====================================================

export const insertSportSchema = createInsertSchema(sports).omit({ id: true });
export type InsertSport = z.infer<typeof insertSportSchema>;

export const insertLeagueSchema = createInsertSchema(leagues).omit({ id: true });
export type InsertLeague = z.infer<typeof insertLeagueSchema>;

export const insertTeamSchema = createInsertSchema(teams).omit({ id: true });
export type InsertTeam = z.infer<typeof insertTeamSchema>;

export const insertGameSchema = createInsertSchema(games).omit({
  id: true,
  homeScore: true,
  awayScore: true,
});
export type InsertGame = z.infer<typeof insertGameSchema>;

export const insertPredictionSchema = createInsertSchema(predictions).omit({
  id: true,
  createdAt: true,
  isFinal: true,
  winner: true,
});
export type InsertPrediction = z.infer<typeof insertPredictionSchema>;

// =====================================================
// EXPLICIT API CONTRACT TYPES
// =====================================================

// Games
export type CreateGameRequest = InsertGame;
export type UpdateGameRequest = Partial<InsertGame> &
  Partial<Pick<Game, "homeScore" | "awayScore" | "status">>;

export type GameResponse = Game;
export type GamesListResponse = Game[];

// Predictions
export type CreatePredictionRequest = {
  gameId: number;
};

export type PredictionResponse = Prediction;
export type PredictionsListResponse = Prediction[];

// “Engine” endpoint response (composed view)
export interface GameWithTeams {
  id: number;
  leagueId: number;
  startTime: string; // ISO
  status: string;
  homeScore: number | null;
  awayScore: number | null;
  homeTeam: { id: number; name: string; shortName: string; slug: string };
  awayTeam: { id: number; name: string; shortName: string; slug: string };
  latestPrediction?: {
    id: number;
    createdAt: string; // ISO
    homeWinProb: number;
    awayWinProb: number;
    drawProb: number | null;
    recommendedPick: string | null;
    isFinal: boolean;
    winner: string | null;
  };
}

export type GamesWithPredictionsResponse = GameWithTeams[];

export interface SeedStatusResponse {
  seeded: boolean;
  counts: {
    sports: number;
    leagues: number;
    teams: number;
    games: number;
    predictions: number;
  };
}
