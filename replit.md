# replit.md

## Overview

This is a **sports prediction engine** web application. It allows users to manage games (primarily soccer/football), run a deterministic prediction engine on those games, and view probability-based outcomes (home win, away win, draw) on a dashboard. The app fetches live fixture data from an external football API and stores games, teams, leagues, and predictions in a PostgreSQL database.

The project follows a monorepo structure with a React frontend (`client/`), an Express backend (`server/`), and shared types/schemas (`shared/`).

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Monorepo Layout

```
client/          → React SPA (Vite + TypeScript)
server/          → Express API server (TypeScript, tsx runner)
shared/          → Shared schemas (Drizzle ORM tables, Zod validators, API route definitions)
migrations/      → Drizzle-generated SQL migrations
attached_assets/ → Reference files (Python scripts, design docs — not part of the running app)
```

### Frontend (client/)

- **Framework**: React 18 with TypeScript
- **Bundler**: Vite (config in `vite.config.ts`)
- **Routing**: Wouter (lightweight client-side router)
- **State/Data Fetching**: TanStack React Query for server state management
- **UI Components**: shadcn/ui (new-york style) built on Radix UI primitives with Tailwind CSS
- **Styling**: Tailwind CSS with CSS custom properties for theming; dark-mode first design ("night terminal luxe" aesthetic)
- **Fonts**: IBM Plex Sans (body), Fraunces (display/serif headings)
- **Form Handling**: React Hook Form + Zod resolvers
- **Key Pages**:
  - `/` — Dashboard with engine predictions, filtering, seeding
  - `/games` — CRUD management for games
  - `/about` — Info/about page
- **Path Aliases**: `@/` → `client/src/`, `@shared/` → `shared/`, `@assets/` → `attached_assets/`

### Backend (server/)

- **Runtime**: Node.js with Express
- **Dev runner**: `tsx` for TypeScript execution without compilation
- **API Pattern**: REST endpoints defined in `shared/routes.ts` with Zod input/output validation; route handlers in `server/routes.ts`
- **Storage Layer**: `server/storage.ts` implements `IStorage` interface with methods for games, predictions, teams, leagues, and admin seeding
- **Data Fetching**: `server/fetcher.ts` pulls fixtures from the football API (`v3.football.api-sports.io`) and upserts into the database
- **Dev Server**: Vite middleware served through Express in development (`server/vite.ts`); static files in production (`server/static.ts`)
- **Build**: Custom build script (`script/build.ts`) uses Vite for client and esbuild for server, outputting to `dist/`

### Database

- **Database**: PostgreSQL (required, connected via `DATABASE_URL` environment variable)
- **ORM**: Drizzle ORM with `drizzle-zod` for automatic Zod schema generation from table definitions
- **Schema** (`shared/schema.ts`):
  - `users` — Basic auth users (id, username, password)
  - `sports` — Sport categories (e.g., Soccer)
  - `leagues` — Leagues within sports
  - `teams` — Teams within leagues
  - `games` — Individual matches with home/away teams, scores, status, start times
  - `predictions` — Engine-generated predictions with home/draw/away probabilities and recommended picks
- **Migrations**: Managed via `drizzle-kit push` (schema push approach, not migration files)
- **Connection**: `pg` Pool in `server/db.ts`

### Shared Layer (shared/)

- `schema.ts` — All Drizzle table definitions, Zod insert schemas, and TypeScript types
- `routes.ts` — Centralized API route definitions with method, path, Zod input/output schemas. Used by both client (for type-safe fetching) and server (for validation). Includes a `buildUrl` helper for parameterized routes.

### Key Design Decisions

1. **Shared route contracts**: API routes are defined once in `shared/routes.ts` with full Zod typing, ensuring client and server stay in sync without codegen.
2. **Deterministic predictions (v1)**: The prediction engine is currently deterministic (not ML-based), computing probabilities from game data in the storage layer.
3. **Seed-on-empty pattern**: The dashboard auto-detects empty state and offers a seed button (`POST /api/admin/seed`) to populate demo data.
4. **Dark-mode default**: The ThemeProvider defaults to dark mode with localStorage persistence.

## External Dependencies

### Required Services

- **PostgreSQL Database**: Connected via `DATABASE_URL` environment variable. Used for all persistent storage. Must be provisioned before the app starts.
- **Football API** (`v3.football.api-sports.io`): External REST API for fetching real football fixtures. Requires `FOOTBALL_API_KEY` environment variable. Used in `server/fetcher.ts` for daily fixture imports.

### Key npm Dependencies

- **Server**: express, drizzle-orm, pg, connect-pg-simple, express-session, passport, zod, axios
- **Client**: react, wouter, @tanstack/react-query, react-hook-form, recharts, shadcn/ui (Radix primitives), tailwindcss
- **Shared**: drizzle-zod, zod

### Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `FOOTBALL_API_KEY` | No (for fetcher) | API key for football.api-sports.io |