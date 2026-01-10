#!/usr/bin/env python3
"""
Interactive match resolver and AI runner.

- Lists fixtures for the next N days from titan stats.
- Shows kickoff, league, home/away, and pairing status (titan/hkjc/macau).
- Lets you choose which fixtures to run AI on.
- Writes out_ai_auto/auto_matches.json for the selected fixtures (plus prior results from the SAME week in the same out dir).
- Optionally runs ai_runner immediately, reusing cached AI responses for prior matches in that week.

Requires:
- common_utils.py (load_titan_stats, load_latest_hkjc_odds, load_latest_macau_odds,
  compute_matches, get_kickoff_datetime)
- ai_runner.py (imports main_async)
"""

import argparse
import asyncio
import json
import subprocess
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional

from common_utils import (
    load_titan_stats,
    load_latest_hkjc_odds,
    load_latest_macau_odds,
    compute_matches,
    get_kickoff_datetime,
)

import ai_runner

# Paths and staleness thresholds
MACAU_DIR = Path("/home/andy/aitest/macauslot/odds")
HKJC_DIR = Path("/home/andy/aitest/hkjc/odds")
TITAN_BASE = Path("/home/andy/aitest/titan/stats/full")
STALE_HOURS_MACAU = 6
STALE_HOURS_HKJC = 6


def newest_mtime(dirpath: Path, pattern: str) -> float:
    times = [p.stat().st_mtime for p in dirpath.glob(pattern)]
    return max(times) if times else 0


def ensure_latest_feeds():
    now = time.time()

    macau_mtime = newest_mtime(MACAU_DIR, "market_data_complete_*.json")
    if macau_mtime == 0 or now - macau_mtime > STALE_HOURS_MACAU * 3600:
        print("Refreshing Macau odds...")
        subprocess.run(
            ["python3", "macauslotscraper.py", "--output", str(MACAU_DIR.parent)],
            check=True,
        )

    hkjc_mtime = newest_mtime(HKJC_DIR, "*.json")
    if hkjc_mtime == 0 or now - hkjc_mtime > STALE_HOURS_HKJC * 3600:
        print("Refreshing HKJC odds...")
        subprocess.run(
            ["python3", "hkjcscraper.py", "--concurrency", "6"],
            check=True,
        )


def ensure_titan_stats(titan_id: str) -> bool:
    stats_path = TITAN_BASE / f"{titan_id}.json"
    if stats_path.exists():
        return False  # already have it, skip
    subprocess.run(
        [
            "python3",
            "titanstatscraper.py",
            "--id",
            titan_id,
            "--base",
            str(TITAN_BASE),
            "--skip-existing",
        ],
        check=True,
    )
    return True


def load_previous_ai(out_dir: Path) -> Dict[str, Dict]:
    """Load prior ai_summary_*.json files and return a map titan_id -> record."""
    out: Dict[str, Dict] = {}
    for f in sorted(out_dir.glob("ai_summary_*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            if isinstance(data.get("items"), list):
                items = data["items"]
            elif isinstance(data.get("matches"), list):
                items = data["matches"]
            else:
                items = [data] if ("titan_id" in data or "match_id" in data or "id" in data) else []
        for it in items:
            tid = str(it.get("titan_id") or it.get("match_id") or it.get("id") or "")
            if not tid:
                continue
            out[tid] = {
                "titan_id": tid,
                "hkjc_event_id": it.get("hkjc_event_id"),
                "macau_event_id": it.get("macau_event_id"),
            }
    return out


def load_fixtures(days: int, dt_tol_minutes: int, macau_lookback_days: int, macau_max_files: int) -> List[Dict]:
    titan_map = load_titan_stats()
    hkjc_map = load_latest_hkjc_odds()
    macau_map = load_latest_macau_odds(
        lookback_days=macau_lookback_days,
        max_files=macau_max_files,
    )

    pairings_list = compute_matches(
        titan_map,
        hkjc_map,
        macau_map,
        thresh_avg=0.40,
        thresh_side=0.0,
        dt_tol_minutes=dt_tol_minutes,
    )
    pairings = {p["titan_id"]: p for p in pairings_list}

    today = date.today()
    end_date = today + timedelta(days=days - 1)

    fixtures = []
    for tid, stats in titan_map.items():
        dt_kick = get_kickoff_datetime(stats)
        if not dt_kick:
            continue
        d = dt_kick.date()
        if d < today or d > end_date:
            continue
        match_block = stats.get("match") or {}
        league = (
            match_block.get("competition")
            or stats.get("league")
            or stats.get("competition")
            or ""
        )
        home = (
            match_block.get("home_team")
            or stats.get("home_team")
            or match_block.get("home_name")
            or ""
        )
        away = (
            match_block.get("away_team")
            or stats.get("away_team")
            or match_block.get("away_name")
            or ""
        )
        kickoff_str = dt_kick.strftime("%Y-%m-%d %H:%M")
        pairing = pairings.get(tid, {})
        fixtures.append(
            {
                "titan_id": tid,
                "kickoff": kickoff_str,
                "kickoff_dt": dt_kick,
                "league": league,
                "home": home,
                "away": away,
                "hkjc_event_id": pairing.get("hkjc_event_id"),
                "macau_event_id": pairing.get("macau_event_id"),
            }
        )

    fixtures.sort(key=lambda x: x["kickoff_dt"])
    return fixtures


def format_fixture(idx: int, f: Dict) -> str:
    t = f["kickoff"]
    league = f.get("league", "") or "?"
    home = f.get("home", "?")
    away = f.get("away", "?")
    tid = f.get("titan_id")
    hkjc = "ok" if f.get("hkjc_event_id") else "MISSING"
    macau = "ok" if f.get("macau_event_id") else "MISSING"
    return (
        f"[{idx}] {t} | {league} | {home} vs {away} | "
        f"titan:{tid} hkjc:{hkjc} macau:{macau}"
    )


def pick_day(days: List[date]) -> Optional[date]:
    while True:
        print("\nPick a day:")
        for i, d in enumerate(days, 1):
            print(f"  {i}) {d.isoformat()}")
        sel = input("Enter day number (or 'q' to quit): ").strip().lower()
        if sel in ("q", "quit", "exit"):
            return None
        try:
            day_idx = int(sel)
            if 1 <= day_idx <= len(days):
                return days[day_idx - 1]
        except Exception:
            pass
        print("Invalid selection. Try again.")


def pick_matches(n: int) -> List[int]:
    while True:
        raw = input("Select matches (e.g., 1,3,5 or 'all' or 'skip'): ").strip().lower()
        if raw in ("skip", "s", ""):
            return []
        if raw == "all":
            return list(range(1, n + 1))
        try:
            nums = [int(x) for x in raw.replace(" ", "").split(",") if x]
            if all(1 <= x <= n for x in nums):
                return nums
        except Exception:
            pass
        print("Invalid selection. Try again.")


def warn_missing(f: Dict):
    missing = []
    if not f.get("hkjc_event_id"):
        missing.append("hkjc_event_id")
    if not f.get("macau_event_id"):
        missing.append("macau_event_id")
    if missing:
        print(f"  Missing: {', '.join(missing)}")


def write_auto_matches(fixtures: List[Dict], out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "auto_matches.json"
    payload = [
        {
            "titan_id": f["titan_id"],
            "hkjc_event_id": f.get("hkjc_event_id"),
            "macau_event_id": f.get("macau_event_id"),
        }
        for f in fixtures
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {path} (matches={len(payload)})")


def week_bounds(d: date) -> (date, date):
    start = d - timedelta(days=d.weekday())  # Monday
    end = start + timedelta(days=6)
    return start, end


async def maybe_run_ai(out_dir: Path, force: bool, selected: List[Dict], include_prior: bool, titan_map: Dict[str, Dict]):
    yn = input("Run AI now? (y/N): ").strip().lower()
    if yn != "y":
        print("Skipping AI run.")
        return

    # Determine target ISO week from current selection
    selected_dates = [f["kickoff_dt"].date() for f in selected if f.get("kickoff_dt")]
    if not selected_dates:
        print("No kickoff dates for selection; running AI on current selection only.")
        prior_filtered = {}
    else:
        ws, we = week_bounds(min(selected_dates))
        prior = load_previous_ai(out_dir) if include_prior else {}
        prior_filtered = {}
        for tid, rec in prior.items():
            stats = titan_map.get(tid)
            if not stats:
                continue
            dtk = get_kickoff_datetime(stats)
            if not dtk:
                continue
            d = dtk.date()
            if ws <= d <= we:
                prior_filtered[tid] = rec

    merged = {f["titan_id"]: f for f in selected}
    for tid, rec in prior_filtered.items():
        if tid not in merged:
            merged[tid] = {
                "titan_id": tid,
                "hkjc_event_id": rec.get("hkjc_event_id"),
                "macau_event_id": rec.get("macau_event_id"),
            }

    write_auto_matches(list(merged.values()), out_dir)

    excel_path = Path("ai_report.xlsx")
    await ai_runner.main_async(out_dir, excel_path, force=force)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="How many days ahead to show (default: 7)")
    ap.add_argument("--out", default="out_ai_auto", help="Output dir (writes auto_matches.json)")
    ap.add_argument("--force", action="store_true", help="Force re-run AI even if cached")
    ap.add_argument("--no-refresh", action="store_true", help="Skip auto-refresh of Macau/HKJC odds")
    ap.add_argument("--dt-tol-minutes", type=int, default=180, help="Time tolerance for matching (minutes)")
    ap.add_argument("--macau-lookback-days", type=int, default=5, help="Macau odds lookback days for merged loading")
    ap.add_argument("--macau-max-files", type=int, default=12, help="Max Macau files to merge when loading odds")
    ap.add_argument("--include-prior-ai", action="store_true", default=True, help="Include prior AI results in this out dir when regenerating Excel (same ISO week only)")
    args = ap.parse_args()

    if not args.no_refresh:
        ensure_latest_feeds()

    fixtures = load_fixtures(
        days=args.days,
        dt_tol_minutes=args.dt_tol_minutes,
        macau_lookback_days=args.macau_lookback_days,
        macau_max_files=args.macau_max_files,
    )
    if not fixtures:
        print("No fixtures found in the next days.")
        return

    # Titan map for week-filtering of prior AI
    titan_map_for_ai = load_titan_stats()

    day_list = sorted({f["kickoff_dt"].date() for f in fixtures})
    while True:
        day = pick_day(day_list)
        if day is None:
            break
        day_fixtures = [f for f in fixtures if f["kickoff_dt"].date() == day]
        if not day_fixtures:
            print("No fixtures for that day.")
            continue

        print(f"\nFixtures for {day}:")
        for i, f in enumerate(day_fixtures, 1):
            print(format_fixture(i, f))

        sel = pick_matches(len(day_fixtures))
        if not sel:
            print("No matches selected.")
            continue

        selected = []
        missing_ids = []
        for idx in sel:
            f = day_fixtures[idx - 1]
            print(f"\nSelected: {format_fixture(idx, f)}")
            warn_missing(f)
            if not (TITAN_BASE / f"{f['titan_id']}.json").exists():
                missing_ids.append(f["titan_id"])
            selected.append(f)

        if missing_ids:
            print(f"Fetching {len(missing_ids)} missing Titan stats: {missing_ids}")
            for tid in missing_ids:
                ensure_titan_stats(tid)

        write_auto_matches(selected, Path(args.out))
        asyncio.run(
            maybe_run_ai(
                Path(args.out),
                force=args.force,
                selected=selected,
                include_prior=args.include_prior_ai,
                titan_map=titan_map_for_ai,
            )
        )

        again = input("Pick another day? (y/N): ").strip().lower()
        if again != "y":
            break


if __name__ == "__main__":
    main()
