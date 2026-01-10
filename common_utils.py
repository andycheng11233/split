import json, re, time
from datetime import datetime
from pathlib import Path
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple


# Paths
HKJC_DIR = Path("/home/andy/aitest/hkjc/odds")
MACAU_DIR = Path("/home/andy/aitest/macauslot/odds")
MACAU_DIR_FALLBACK = None
TITAN_DIR = Path("/home/andy/aitest/titan/stats/full")
ALIAS_FILE = Path("alias.json")
UNALIAS_PENDING_FILE = Path("unalias_pending.json")

PLACEHOLDER_TEAM_NAMES = {s.lower() for s in ["關閉", "閉", "tbd", "待定", "unknown", ""]}

# ---------- Alias handling ----------
def load_aliases():
    try:
        return json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"teams": {}}

aliases = load_aliases()

def append_unalias_pending(raw: str):
    if not raw:
        return
    try:
        pending = json.loads(UNALIAS_PENDING_FILE.read_text(encoding="utf-8"))
    except Exception:
        pending = {"teams": []}
    if raw not in pending.get("teams", []):
        pending.setdefault("teams", []).append(raw)
        UNALIAS_PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")

def resolve_team(name: str, source: str) -> str:
    if not name:
        return ""
    nm = name.strip().lower()
    if nm in PLACEHOLDER_TEAM_NAMES:
        return ""
    for canon, data in (aliases.get("teams") or {}).items():
        for v in data.get("variants", []):
            if v.strip().lower() == nm:
                return canon
    append_unalias_pending(name)
    return name.strip()

# ---------- Name/time helpers ----------
def norm_name(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s)
    return " ".join(s.split())

def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()

def _parse_datetime(dt_str: str) -> Optional[datetime]:
    if not dt_str or not isinstance(dt_str, str):
        return None
    s = dt_str.strip().replace("T", " ").replace("\u00a0", " ")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})", s)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            return datetime(y, mo, d, h, mi)
        except Exception:
            return None
    m = re.match(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", s)
    if m:
        d, mo, h, mi = map(int, m.groups())
        try:
            return datetime(datetime.now().year, mo, d, h, mi)
        except Exception:
            return None
    return None

def extract_time(obj: Dict[str, Any]) -> Optional[str]:
    for key in ["match_time", "time", "time_raw", "game_time", "kickoff"]:
        v = obj.get(key)
        if not v and isinstance(obj.get("standard"), dict):
            v = obj["standard"].get("kickoff")
        if not v and isinstance(obj.get("match"), dict):
            v = obj["match"].get(key) or obj["match"].get("kickoff")
        if isinstance(v, str):
            m = re.search(r"\b(\d{1,2}):(\d{2})\b", v)
            if m:
                hh, mm = m.groups()
                try:
                    hh_i, mm_i = int(hh), int(mm)
                    if 0 <= hh_i < 24 and 0 <= mm_i < 60:
                        return f"{hh_i:02d}:{mm_i:02d}"
                except Exception:
                    pass
    return None

def get_kickoff_datetime(stats: Dict[str, Any]) -> Optional[datetime]:
    m = stats.get("match") or {}
    for key in ("datetime", "kickoff"):
        dt = _parse_datetime(m.get(key))
        if dt:
            return dt
    date_fields = [
        (stats.get("match_date"), stats.get("match_time")),
        (m.get("kickoff_date"), m.get("kickoff_time")),
        ((stats.get("standard") or {}).get("kickoff_date"), (stats.get("standard") or {}).get("kickoff_time")),
    ]
    for d, t in date_fields:
        if d and t and isinstance(d, str) and isinstance(t, str):
            dt = _parse_datetime(f"{d.strip()} {t.strip()}")
            if dt:
                return dt
    return _parse_datetime(stats.get("game_time") or "")

def get_odds_datetime(obj: Dict[str, Any]) -> Optional[datetime]:
    std = obj.get("standard") or {}
    m = obj.get("match") or {}
    for candidate in [
        std.get("kickoff"),
        m.get("time_raw"),
        obj.get("kickoff"),
        obj.get("time_raw"),
    ]:
        dt = _parse_datetime(candidate)
        if dt:
            return dt
    d = std.get("kickoff_date") or obj.get("match_date")
    t = std.get("kickoff_time") or obj.get("match_time")
    if d and t:
        return _parse_datetime(f"{d} {t}")
    return None

def times_match(t_time: Optional[str], o_time: Optional[str]) -> bool:
    return bool(t_time and o_time and t_time == o_time)

def times_within_minutes(a: datetime, b: datetime, minutes: int = 90) -> bool:
    return abs((a - b).total_seconds()) <= minutes * 60

# ---------- Matching ----------
def best_match(
    t_home: str,
    t_away: str,
    t_time: Optional[str],
    t_dt: Optional[datetime],
    pool: Dict[str, Dict[str, Any]],
    source_tag: str,
    thresh_avg: float = 0.40,
    thresh_side: float = 0.0,
    dt_tol_minutes: int = 180,
) -> Optional[str]:
    if not t_home or not t_away:
        return None

    best_id, best_score = None, 0.0
    thn, tan = norm_name(t_home), norm_name(t_away)

    for eid, obj in pool.items():
        home = resolve_team(
            obj.get("home_team", "") or obj.get("match", {}).get("home_team", "") or (obj.get("standard") or {}).get("home_team_raw", ""),
            source=source_tag
        )
        away = resolve_team(
            obj.get("away_team", "") or obj.get("match", {}).get("away_team", "") or (obj.get("standard") or {}).get("away_team_raw", ""),
            source=source_tag
        )
        if not home or not away:
            continue
        h = norm_name(home)
        a = norm_name(away)

        o_dt = get_odds_datetime(obj)
        o_time = extract_time(obj)

        s_home = sim(thn, h)
        s_away = sim(tan, a)
        avg_direct = (s_home + s_away) / 2

        s_home_sw = sim(thn, a)
        s_away_sw = sim(tan, h)
        avg_swapped = (s_home_sw + s_away_sw) / 2

        use_swapped = avg_swapped > avg_direct
        avg_sim = avg_swapped if use_swapped else avg_direct
        side_min = min(s_home_sw, s_away_sw) if use_swapped else min(s_home, s_away)

        exact_match = False
        if use_swapped:
            exact_match = (s_home_sw == 1.0) or (s_away_sw == 1.0)
        else:
            exact_match = (s_home == 1.0) or (s_away == 1.0)

        if not exact_match:
            if t_dt and o_dt:
                if not times_within_minutes(t_dt, o_dt, minutes=dt_tol_minutes):
                    continue
            elif t_time and o_time:
                if not times_match(t_time, o_time):
                    continue
            else:
                continue

        if avg_sim < thresh_avg or side_min < thresh_side:
            continue

        final_score = avg_sim
        if final_score > best_score:
            best_id, best_score = eid, final_score

    return best_id

def compute_matches(titan_map, hkjc_map, macau_map, target_ids: Optional[set] = None,
                    thresh_avg=0.40, thresh_side=0.0, dt_tol_minutes=180):
    res = []
    for tid, stats in titan_map.items():
        if target_ids is not None and tid not in target_ids:
            continue
        t_home_raw = stats.get("home_team") or stats.get("match", {}).get("home_team") or ""
        t_away_raw = stats.get("away_team") or stats.get("match", {}).get("away_team") or ""
        t_home = resolve_team(t_home_raw, source="titan")
        t_away = resolve_team(t_away_raw, source="titan")
        t_time = extract_time(stats)
        t_dt = get_kickoff_datetime(stats)
        hid = best_match(t_home, t_away, t_time, t_dt, hkjc_map, source_tag="hkjc",
                         thresh_avg=thresh_avg, thresh_side=thresh_side, dt_tol_minutes=dt_tol_minutes)
        mid = best_match(t_home, t_away, t_time, t_dt, macau_map, source_tag="macauslot",
                         thresh_avg=thresh_avg, thresh_side=thresh_side, dt_tol_minutes=dt_tol_minutes)
        res.append({"titan_id": tid, "hkjc_event_id": hid, "macau_event_id": mid})
    return res

# ---------- Loaders ----------
def load_latest_hkjc_odds(odds_dir: Path = HKJC_DIR) -> Dict[str, Dict[str, Any]]:
    by_event = {}
    if not odds_dir.exists():
        return by_event
    files = sorted(odds_dir.glob("hkjc_odds_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            eid = str(data.get("event_id") or data.get("match", {}).get("match_id") or "")
            if not eid or eid in by_event:
                continue
            by_event[eid] = data
        except Exception:
            continue
    return by_event

def load_latest_macau_odds(
    primary_dir: Path = MACAU_DIR,
    fallback_dir: Optional[Path] = MACAU_DIR_FALLBACK,
    lookback_days: int = 5,
    max_files: int = 12,
) -> Dict[str, Dict[str, Any]]:
    """
    Load Macau odds by merging market_data_complete_*.json files from the last
    `lookback_days`. If none are within the window, fall back to the most
    recent `max_files`. Newer files win on duplicate event_id.
    """
    cutoff = time.time() - lookback_days * 86400

    def gather(dirpath: Optional[Path]):
        if not dirpath or not dirpath.exists():
            return []
        files = sorted(
            dirpath.glob("market_data_complete_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        recent = [p for p in files if p.stat().st_mtime >= cutoff]
        return (recent or files)[:max_files]

    candidates, seen = [], set()
    for f in gather(primary_dir) + gather(fallback_dir):
        if f not in seen:
            candidates.append(f)
            seen.add(f)

    res: Dict[str, Dict[str, Any]] = {}
    for f in candidates:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            evs = data.get("events")
            iterable = evs.values() if isinstance(evs, dict) else evs if isinstance(evs, list) else []
            for ev in iterable:
                mid = str(ev.get("event_id") or ev.get("match_id") or ev.get("id") or "")
                if mid and mid not in res:  # keep first (newest) occurrence
                    res[mid] = ev
        except Exception:
            continue
    return res

def load_titan_stats(full_dir: Path = TITAN_DIR) -> Dict[str, Dict[str, Any]]:
    out = {}
    if not full_dir.exists():
        return out
    for f in full_dir.glob("*.json"):
        try:
            tid = f.stem
            out[tid] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
    return out

# ---------- Readiness helpers ----------
def has_meaningful_data_for_ai(normalized_stats: Dict[str, Any]) -> bool:
    for k, v in normalized_stats.items():
        if k.startswith("_"):
            continue
        if v:
            return True
    meta = normalized_stats.get("_meta", {}) or {}
    if meta.get("available_sections"):
        return True
    rr = normalized_stats.get("recent10_ratings_parsed") or {}
    return bool(
        rr.get("home_recent_ratings")
        or rr.get("away_recent_ratings")
        or rr.get("home_recent_average")
        or rr.get("away_recent_average")
    )

def readiness_check(normalized: dict, odds_bundle: dict, api_key_present: bool) -> Tuple[bool, List[str]]:
    reasons = []
    # Removed the "no_odds" gate to allow running with HKJC-only, Macau-only, or no odds.
    if not has_meaningful_data_for_ai(normalized):
        reasons.append("no_stats")
    if not api_key_present:
        reasons.append("no_api_key")
    return (len(reasons) == 0, reasons)
