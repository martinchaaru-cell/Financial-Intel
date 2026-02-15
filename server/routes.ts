import type { Express } from "express";
import type { Server } from "http";
import { z } from "zod";
import { api } from "@shared/routes";
import { storage } from "./storage";

export async function registerRoutes(
  httpServer: Server,
  app: Express,
): Promise<Server> {
  // Engine summary view
  app.get(api.engine.gamesWithPredictions.path, async (req, res) => {
    const input = api.engine.gamesWithPredictions.input?.parse(req.query);
    const filters = {
      leagueId: input?.leagueId,
      status: input?.status,
      from: input?.from ? new Date(input.from) : undefined,
      to: input?.to ? new Date(input.to) : undefined,
      sortBy: input?.sortBy as "date" | "probability",
    };

    const games = await storage.listGamesWithTeamsAndLatestPrediction(filters);
    res.json(games);
  });

  // Games
  app.get(api.games.list.path, async (req, res) => {
    const input = api.games.list.input?.parse(req.query);
    const games = await storage.listGames({
      leagueId: input?.leagueId,
      status: input?.status,
    });
    res.json(games);
  });

  app.get(api.games.get.path, async (req, res) => {
    const id = Number(req.params.id);
    const game = await storage.getGame(id);
    if (!game) {
      return res.status(404).json({ message: "Game not found" });
    }
    res.json(game);
  });

  app.post(api.games.create.path, async (req, res) => {
    try {
      const input = api.games.create.input.parse(req.body);
      const created = await storage.createGame({
        ...input,
        startTime: input.startTime,
      } as any);
      res.status(201).json(created);
    } catch (err) {
      if (err instanceof z.ZodError) {
        return res.status(400).json({
          message: err.errors[0]?.message ?? "Invalid request",
          field: err.errors[0]?.path?.join("."),
        });
      }
      throw err;
    }
  });

  app.put(api.games.update.path, async (req, res) => {
    try {
      const id = Number(req.params.id);
      const input = api.games.update.input.parse(req.body);
      const updated = await storage.updateGame(id, input as any);
      if (!updated) {
        return res.status(404).json({ message: "Game not found" });
      }
      res.json(updated);
    } catch (err) {
      if (err instanceof z.ZodError) {
        return res.status(400).json({
          message: err.errors[0]?.message ?? "Invalid request",
          field: err.errors[0]?.path?.join("."),
        });
      }
      throw err;
    }
  });

  app.delete(api.games.delete.path, async (req, res) => {
    const id = Number(req.params.id);
    const ok = await storage.deleteGame(id);
    if (!ok) {
      return res.status(404).json({ message: "Game not found" });
    }
    res.status(204).send();
  });

  // Predictions for a game
  app.get(api.predictions.listForGame.path, async (req, res) => {
    const gameId = Number(req.params.id);
    const game = await storage.getGame(gameId);
    if (!game) {
      return res.status(404).json({ message: "Game not found" });
    }
    const preds = await storage.listPredictionsForGame(gameId);
    res.json(preds);
  });

  app.post(api.predictions.createForGame.path, async (req, res) => {
    const gameId = Number(req.params.id);
    const game = await storage.getGame(gameId);
    if (!game) {
      return res.status(404).json({ message: "Game not found" });
    }

    const pred = await storage.createPrediction({ gameId });
    res.status(201).json(pred);
  });

  // Admin fetch daily fixtures
  app.post("/api/admin/fetch-fixtures", async (req, res) => {
    const date = req.body.date || new Date().toISOString().split('T')[0];
    try {
      const { fetchDailyFixtures } = await import("./fetcher");
      await fetchDailyFixtures(date);
      res.json({ message: `Fixtures for ${date} fetched and predicted.` });
    } catch (err: any) {
      res.status(500).json({ message: err.message });
    }
  });

  return httpServer;
}
