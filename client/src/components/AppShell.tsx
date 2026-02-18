import { PropsWithChildren } from "react";
import { Link, useLocation } from "wouter";
import {
  Activity,
  CalendarClock,
  Info,
  Moon,
  Sun,
  Sparkles,
  ShieldCheck,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useTheme } from "@/components/ThemeProvider";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";

function NavItem({
  href,
  icon,
  label,
  testId,
}: {
  href: string;
  icon: React.ReactNode;
  label: string;
  testId: string;
}) {
  const [loc] = useLocation();
  const active = loc === href;

  return (
    <Link
      href={href}
      data-testid={testId}
      className={cn(
        "group flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-semibold transition-all duration-200 focus:outline-none focus-visible:ring-4 focus-visible:ring-ring/20",
        active
          ? "bg-gradient-to-r from-primary/18 to-sidebar-accent/14 text-foreground shadow-sm shadow-black/40 border border-border/70"
          : "text-muted-foreground hover:text-foreground hover:bg-white/5 border border-transparent",
      )}
    >
      <span
        className={cn(
          "grid h-9 w-9 place-items-center rounded-lg border transition-all duration-200",
          active
            ? "border-border/70 bg-white/6"
            : "border-border/40 bg-white/3 group-hover:bg-white/6",
        )}
      >
        {icon}
      </span>
      <span className="truncate">{label}</span>
      <span
        className={cn(
          "ml-auto h-1.5 w-1.5 rounded-full transition-opacity",
          active ? "opacity-100 bg-primary" : "opacity-0 bg-primary",
        )}
      />
    </Link>
  );
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const isDark = theme === "dark";
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      data-testid="theme-toggle"
      className={cn(
        "gap-2 rounded-xl border-border/60 bg-white/5 hover:bg-white/8 shadow-sm shadow-black/30",
      )}
    >
      {isDark ? (
        <>
          <Moon className="h-4 w-4" />
          Dark
        </>
      ) : (
        <>
          <Sun className="h-4 w-4" />
          Light
        </>
      )}
    </Button>
  );
}

export default function AppShell({ children }: PropsWithChildren) {
  return (
    <div className="min-h-screen bg-aurora grain">
      <div className="relative z-10">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 py-6 lg:py-10">
          <div className="grid grid-cols-1 lg:grid-cols-[290px_1fr] gap-6 lg:gap-8">
            <aside className="glass rounded-3xl p-4 lg:p-5">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-3">
                  <div className="relative">
                    <div className="absolute inset-0 rounded-2xl bg-gradient-to-br from-primary/40 to-sidebar-accent/30 blur-xl" />
                    <div className="relative grid h-12 w-12 place-items-center rounded-2xl border border-border/60 bg-white/6 shadow-md shadow-black/50">
                      <Sparkles className="h-6 w-6 text-primary" />
                    </div>
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm text-muted-foreground">
                      Sports Engine
                    </div>
                    <div
                      className="text-lg leading-none font-bold"
                      style={{ fontFamily: "var(--font-serif)" }}
                      data-testid="app-title"
                    >
                      <span className="text-gradient">Nightline</span>{" "}
                      Predictions
                    </div>
                  </div>
                </div>
                <div className="hidden lg:block">
                  <ThemeToggle />
                </div>
              </div>

              <Separator className="my-4 bg-border/60" />

              <nav className="space-y-1">
                <NavItem
                  href="/"
                  label="Dashboard"
                  testId="nav-dashboard"
                  icon={<Activity className="h-4.5 w-4.5 text-foreground/90" />}
                />
                <NavItem
                  href="/elite"
                  label="Elite Scanned"
                  testId="nav-elite"
                  icon={<ShieldCheck className="h-4.5 w-4.5 text-primary" />}
                />
                <NavItem
                  href="/games"
                  label="Games"
                  testId="nav-games"
                  icon={
                    <CalendarClock className="h-4.5 w-4.5 text-foreground/90" />
                  }
                />
                <NavItem
                  href="/about"
                  label="About"
                  testId="nav-about"
                  icon={<Info className="h-4.5 w-4.5 text-foreground/90" />}
                />
              </nav>

              <div className="mt-5 lg:hidden">
                <ThemeToggle />
              </div>

              <div className="mt-5 rounded-2xl border border-border/60 bg-white/4 p-4">
                <div className="text-xs font-semibold text-muted-foreground">
                  Tip
                </div>
                <div className="mt-2 text-sm leading-relaxed text-foreground/90">
                  Run the engine on scheduled matches to generate a fresh
                  probability line. Use the Games page to score finals.
                </div>
              </div>
            </aside>

            <main className="min-w-0">{children}</main>
          </div>
        </div>
      </div>
    </div>
  );
}
