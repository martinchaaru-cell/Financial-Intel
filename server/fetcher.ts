import axios from "axios";
import { db } from "./db";
import { games, leagues, teams, sports, predictions } from "@shared/schema";
import { eq, and, sql } from "drizzle-orm";
import { storage } from "./storage";

const API_KEY = process.env.FOOTBALL_API_KEY;
const BASE_URL = "https://v3.football.api-sports.io";

export async function fetchDailyFixtures(date: string) {
  if (!API_KEY) throw new Error("FOOTBALL_API_KEY not found");

  const response = await axios.get(`${BASE_URL}/fixtures`, {
    headers: { "x-apisports-key": API_KEY },
    params: { date },
  });

  const fixtures = response.data.response;
  console.log(`Fetched ${fixtures.length} fixtures for ${date}`);

  for (const f of fixtures) {
    const leagueData = f.league;
    const homeData = f.teams.home;
    const awayData = f.teams.away;
    const fixture = f.fixture;

    // 1. Ensure Sport/League exists
    let [league] = await db.select().from(leagues).where(eq(leagues.slug, leagueData.name.toLowerCase().replace(/ /g, '-'))).limit(1);
    if (!league) {
        // Assume soccer for now
        let [sport] = await db.select().from(sports).where(eq(sports.slug, "soccer")).limit(1);
        if (!sport) {
            [sport] = await db.insert(sports).values({ name: "Soccer", slug: "soccer" }).returning();
        }
        [league] = await db.insert(leagues).values({
            sportId: sport.id,
            name: leagueData.name,
            slug: leagueData.name.toLowerCase().replace(/ /g, '-')
        }).returning();
    }

    // 2. Ensure Teams exist
    let [home] = await db.select().from(teams).where(eq(teams.slug, homeData.name.toLowerCase().replace(/ /g, '-'))).limit(1);
    if (!home) {
        [home] = await db.insert(teams).values({
            leagueId: league.id,
            name: homeData.name,
            shortName: homeData.name.substring(0, 3).toUpperCase(),
            slug: homeData.name.toLowerCase().replace(/ /g, '-')
        }).returning();
    }

    let [away] = await db.select().from(teams).where(eq(teams.slug, awayData.name.toLowerCase().replace(/ /g, '-'))).limit(1);
    if (!away) {
        [away] = await db.insert(teams).values({
            leagueId: league.id,
            name: awayData.name,
            shortName: awayData.name.substring(0, 3).toUpperCase(),
            slug: awayData.name.toLowerCase().replace(/ /g, '-')
        }).returning();
    }

    // 3. Upsert Game with EAT time handling
    // EAT is UTC+3
    const fixtureDate = new Date(fixture.date);
    const gameData = {
        leagueId: league.id,
        startTime: fixtureDate,
        homeTeamId: home.id,
        awayTeamId: away.id,
        status: fixture.status.short === "FT" ? "final" : (fixture.status.short === "NS" ? "scheduled" : "live"),
        homeScore: f.goals.home,
        awayScore: f.goals.away,
    };

    let [game] = await db.select().from(games).where(
        and(
            eq(games.homeTeamId, home.id),
            eq(games.awayTeamId, away.id),
            sql`ABS(EXTRACT(EPOCH FROM (${games.startTime} - ${gameData.startTime}::timestamp))) < 43200` // 12 hour window
        )
    ).limit(1);

    if (game) {
        await db.update(games).set(gameData).where(eq(games.id, game.id));
    } else {
        [game] = await db.insert(games).values(gameData).returning();
    }

    // 4. Trigger Prediction Run
    await storage.createPrediction({ gameId: game.id });
  }
}
