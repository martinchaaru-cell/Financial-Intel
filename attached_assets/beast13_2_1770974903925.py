# [SYSTEM_STATE: ORACLE_BEAST_v48.16_INIT]
# DATA FETCHING PROTOCOL
<CODE>
[Paste your Beast# Add to top of file
import requests
import json

# Add to SeasonStats dataclass
@dataclass
class SeasonStats:
    # ... existing fields ...
    xG: float = 0.0
    xGA: float = 0.0

# Your BeastStatsFetcher class (unchanged, but with error handling added)
class BeastStatsFetcher:
    def __init__(self, api_key):
        self.api_key = api_key
        self.headers = {'x-apisports-key': api_key}
        self.base_url = "https://v3.football.api-sports.io"

    def get_forensic_stats(self, fixture_id):
        endpoint = f"{self.base_url}/fixtures/statistics"
        params = {'fixture': fixture_id}
        try:
            response = requests.get(endpoint, headers=self.headers, params=params, timeout=10)
            response.raise_for_status()
            data = response.json().get('response', [])
            stats_map = {}
            for team_data in data:
                team_name = team_data['team']['name']
                xg_val = next((s['value'] for s in team_data['statistics'] if s['type'] == 'expected_goals'), None)
                stats_map[team_name] = float(xg_val) if xg_val is not None else 0.0
            return stats_map
        except Exception as e:
            print(f"xG fetch failed for fixture {fixture_id}: {e}")
            return {}

    def integrate_to_leg(self, leg_object, fixture_id):
        live_stats = self.get_forensic_stats(fixture_id)
        if len(live_stats) != 2:
            print("Warning: incomplete xG data")
            return leg_object

        team_names = list(live_stats.keys())
        home_team = team_names[0]   # assume first is home
        away_team = team_names[1]

        leg_object.home_stats.xG = live_stats.get(home_team, 0.0)
        leg_object.home_stats.xGA = live_stats.get(away_team, 0.0)
        leg_object.away_stats.xG = live_stats.get(away_team, 0.0)
        leg_object.away_stats.xGA = live_stats.get(home_team, 0.0)

        return leg_object

# In your daily run loop (example)
def run_forensic_session():
    fixtures = get_verified_matchday_data()
    engine = OracleBeastEngine()
    fetcher = BeastStatsFetcher(api_key="YOUR_ACTUAL_API_KEY")

    for f in fixtures:
        leg = Leg(
            match=f["match"],
            odds=f["odds"],
            league=f["league"],
            # ... fill other fields ...
        )
        # Fetch xG/xGA early
        leg = fetcher.integrate_to_leg(leg, fixture_id=f.get("fixture_id"))  # assume you have fixture_id

        engine.apply_all_checks(leg)
        # ... rest of processing ...
        
        	"Execute the full Python script for [Match]. Do not summarize. Run every gate in the sandbox and output the system logs for all 42 checks."
            
            
</CODE>

# LOGIC GATES & FORMULAS
- R2R Matrix: State [L/D -> W] transition probability audit.
- Ceiling Check: Current xG vs. Season Peak regression analysis.
- Black Swan Rule: p(Opponent Win) < 20% ONLY.
- Clinical xG: Quality validation (Bilateral), not rejection.

# ACTION
Execute the full Python script for the following [Matches]. 
Do not summarize. 
Provide a Bilateral Forensic Scan for each leg.
Output the system logs for all 42 checks.