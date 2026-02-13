import { Link } from "wouter";
import AppShell from "@/components/AppShell";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Radar } from "lucide-react";

export default function NotFound() {
  return (
    <AppShell>
      <div className="anim-in">
        <div className="glass rounded-3xl p-8 sm:p-10">
          <div className="flex items-start gap-4">
            <div className="relative">
              <div className="absolute inset-0 rounded-3xl bg-gradient-to-br from-primary/40 to-sidebar-accent/30 blur-2xl" />
              <div className="relative grid h-14 w-14 place-items-center rounded-3xl border border-border/60 bg-white/6 shadow-lg shadow-black/60">
                <Radar className="h-7 w-7 text-primary" />
              </div>
            </div>

            <div className="min-w-0">
              <div className="text-sm text-muted-foreground">404</div>
              <h1 className="mt-2 text-3xl sm:text-4xl leading-tight">
                Page not found
              </h1>
              <p className="mt-3 max-w-xl text-muted-foreground">
                The route you requested doesn’t exist in this build. Use the
                navigation or return to the dashboard.
              </p>

              <div className="mt-6 flex flex-col sm:flex-row gap-2">
                <Link href="/" className="inline-flex">
                  <Button
                    data-testid="notfound-home"
                    className="w-full sm:w-auto rounded-xl font-semibold bg-gradient-to-r from-primary to-sidebar-accent text-primary-foreground shadow-lg shadow-primary/20 hover:shadow-xl hover:shadow-primary/25 hover:-translate-y-0.5 active:translate-y-0 transition-all duration-200"
                  >
                    <ArrowLeft className="h-4 w-4 mr-2" />
                    Back to Dashboard
                  </Button>
                </Link>
                <Link href="/games" className="inline-flex">
                  <Button
                    data-testid="notfound-games"
                    variant="outline"
                    className="w-full sm:w-auto rounded-xl border-border/60 bg-white/5 hover:bg-white/8"
                  >
                    Go to Games
                  </Button>
                </Link>
              </div>
            </div>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
