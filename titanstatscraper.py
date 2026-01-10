#!/usr/bin/env python3
"""
Titan Stats Scraper with: 
- PRE-SCAN OPTIMIZATION:  Discover all matches first, then filter
- Persistent skip registry (can be bypassed with --rescrape-all)
- Skip started games by default (can be disabled with --no-skip-started)
- Skip existing by default (can be disabled with --rescrape-all)
"""
import argparse
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set

from playwright.async_api import async_playwright
import openpyxl
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("titan_stat_main")

INDEX_URL = "https://live.titan007.com/indexall_big.aspx"
GAME_FILE = "game.json"
ALIAS_FILE = "alias.json"

BAD_STRING = "暫無數據"
MIN_SECTIONS_FOR_FULL = 3

PLACEHOLDER_NAMES = {"關閉", "closed", "none", ""}

SECTIONS_TO_TRY = [
    ("league_standings", r"聯賽積分排名"),
    ("head_to_head", r"對賽往績"),
    ("data_comparison", r"數據對比"),
    ("referee_stats", r"裁判統計"),
    ("league_trend", r"聯賽盤路走勢"),
    ("same_trend", r"相同盤路"),
    ("goal_distribution", r"入球數/上下半場入球分布"),
    ("halftime_fulltime", r"半全場"),
    ("goal_count", r"進球數/單雙"),
    ("goal_time", r"進球時間"),
    ("future_matches", r"未來五場"),
    ("pre_match_brief", r"賽前簡報"),
    ("season_stats_comparison", r"本賽季數據統計比較"),
    ("recent_form", r"近期戰績"),
    ("last_match_player_ratings", r"球員上一場出場評分"),
    ("lineup_and_injuries", r"陣容情況"),
    ("pre_match_table", r"賽前積分榜"),
]


def _norm(val: Any) -> str:
    s = str(val).strip() if val is not None else ""
    return "" if s.lower() in {"", "null", "none", "undefined"} else s


def _norm_key(val: str) -> str:
    return re.sub(r"\s+", "", val.lower()) if val else ""


def load_alias_map() -> Dict[str, str]:
    p = Path(ALIAS_FILE)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    aliases = {}
    for cat in ("teams",):
        for canon, entry in (data.get(cat) or {}).items():
            key = _norm_key(canon)
            if key:
                aliases[key] = canon
            for v in (entry or {}).get("variants", []) or []:
                k2 = _norm_key(v)
                if k2:
                    aliases[k2] = canon
            for src, lst in ((entry or {}).get("sources") or {}).items():
                for v in lst or []:
                    k3 = _norm_key(v)
                    if k3:
                        aliases[k3] = canon
    return aliases


def alias_match(name: str, alias_map: Dict[str, str]) -> str:
    k = _norm_key(name)
    return alias_map.get(k, "")


def load_gamejson():
    with open(GAME_FILE, encoding="utf-8") as f:
        ls = json.load(f)
    gids, subids, simp_trad = set(), set(), set()
    for x in ls:
        sclass = _norm(x.get("SclassID"))
        sub = _norm(x.get("SubID"))
        simp = _norm(x.get("simp"))
        trad = _norm(x.get("trad"))
        if sclass: 
            gids.add(sclass)
        if sub:
            subids.add(sub)
        if simp:
            simp_trad.add(simp)
        if trad:
            simp_trad.add(trad)
    return gids, subids, simp_trad


# =============================================================================
# REGISTRY MANAGEMENT
# =============================================================================
class TitanRegistry:
    """Persistent registry for tracking scraped matches."""

    def __init__(self, registry_path: Path):
        self.registry_path = registry_path
        self.scraped:  Set[str] = self._load()

    def _load(self) -> Set[str]:
        if not self.registry_path.exists():
            return set()
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            return set(data if isinstance(data, list) else [])
        except Exception:
            return set()

    def save(self):
        try:
            self.registry_path.write_text(
                json.dumps(sorted(self.scraped), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"Registry saved:  {self.registry_path}")
        except Exception as e:
            logger.warning(f"Failed to save registry:  {e}")

    def is_scraped(self, match_id: str) -> bool:
        return match_id in self.scraped

    def mark_scraped(self, match_id: str):
        self.scraped.add(match_id)

    def __len__(self):
        return len(self.scraped)


# =============================================================================
# SKIP LOGIC
# =============================================================================
def should_skip_started(game_time: str, buffer_minutes: int = 5) -> bool:
    """
    Check if a match has already started based on game_time. 
    Returns True if the match started more than buffer_minutes ago.
    """
    if not game_time:
        return False

    # Try various datetime formats
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    ]

    kickoff_dt = None
    clean_time = game_time.replace("\u00a0", " ").strip()

    for fmt in formats:
        try:
            kickoff_dt = datetime.strptime(clean_time, fmt)
            break
        except ValueError:
            continue

    if kickoff_dt is None:
        # Try regex extraction
        m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})", clean_time)
        if m:
            try:
                kickoff_dt = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5))
                )
            except Exception:
                return False
        else:
            return False

    now = datetime.now()
    return (now - kickoff_dt) > timedelta(minutes=buffer_minutes)


# =============================================================================
# MATCH DISCOVERY AND FILTERING
# =============================================================================
async def filter_match_ids_only_my_leagues(all_match_ids: List[str]) -> Tuple[List[str], Dict[str, str], Dict[str, str]]: 
    """
    Returns (filtered_ids, league_lookup, game_time_lookup).
    """
    gids, subids, names = load_gamejson()
    filtered:  List[str] = []
    league_lookup: Dict[str, str] = {}
    game_time_lookup: Dict[str, str] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(INDEX_URL, timeout=90000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2200)

        mapping = await page.evaluate("""
            () => {
              let out = {};
              let sources = [window.A, window.arr, window.matchList, window.B, window.C, window.Match];
              for (const src of sources) {
                if (Array.isArray(src)) {
                  for (const row of src) {
                    if (! row) continue;
                    let mid = String(row[0] || row.matchid || row.MatchID || row.matchId || row.id || "");
                    let sclassid = String(row[2] || row[4] || row.sclassid || row.SclassID || row.sclassID || "");
                    let subid = String(row[9] || row.subid || row.SubID || "");
                    let league = row[1] || row.league || "";
                    let simp = row[1] || row.simp || "";
                    let trad = row[1] || row.trad || "";
                    let gameTime = row[3] || row.time || row.gameTime || row.matchTime || "";
                    out[mid] = { sclassid, subid, simp, trad, league, gameTime };
                  }
                }
              }
              return out;
            }
        """)

        for mid in all_match_ids: 
            if not mid.isdigit():
                continue
            info = mapping.get(mid) or {}
            scid = _norm(info.get("sclassid"))
            subid = _norm(info.get("subid"))
            simp = _norm(info.get("simp"))
            trad = _norm(info.get("trad"))
            league = _norm(info.get("league"))
            game_time = _norm(info.get("gameTime"))

            if league:
                league_lookup[mid] = league
            if game_time:
                game_time_lookup[mid] = game_time

            if (scid and scid in gids) or (subid and subid in subids) or (simp and simp in names) or (trad and trad in names):
                filtered.append(mid)
                continue

            tr = await page.query_selector(f'tr[id="tr1_{mid}"]')
            if tr:
                tds = await tr.query_selector_all("td")
                league_name = _norm((await tds[1].inner_text()) if len(tds) > 1 else "")
                if league_name:
                    league_lookup[mid] = league_name
                if league_name and league_name in names:
                    filtered.append(mid)

        await browser.close()
    return filtered, league_lookup, game_time_lookup


def is_real_data_table(tbl):
    rows = tbl.find_all("tr")
    if len(rows) < 2:
        return False
    for row in rows:
        cells = [c.get_text(strip=True) for c in row.find_all(['td', 'th'])]
        for cell in cells:
            if cell and BAD_STRING not in cell: 
                return True
    return False


def extract_table_text(tbl):
    return [
        [cell.get_text(strip=True) for cell in row.find_all(['th', 'td'])]
        for row in tbl.find_all("tr")
    ]


def analyze_sections(html):
    soup = BeautifulSoup(html, "html.parser")
    found, missing = [], []
    debug_info = {}
    tables:  Dict[str, List[List[str]]] = {}

    for key, regex in SECTIONS_TO_TRY:
        header = soup.find(string=re.compile(regex))
        section_status = "not_found"
        if header:
            tbl = header.find_next("table")
            if tbl and is_real_data_table(tbl):
                section_status = "real_data"
                found.append(key)
                tables[key] = extract_table_text(tbl)
            elif tbl: 
                section_status = "only_bad_string"
                missing.append(key)
            else:
                section_status = "table_not_found"
                missing.append(key)
        else:
            missing.append(key)
        debug_info[key] = section_status
    return found, missing, debug_info, tables


def clean_league(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r"\s*第\d+\s*輪.*$", "", name)
    return name.strip()


def _clean_team(name: str) -> str:
    if not name:
        return ""
    name = name.replace("\u00a0", " ")
    name = name.strip()
    if name in PLACEHOLDER_NAMES:
        return ""
    name = re.sub(r"-數據分析-新球體育-球探體育.*", "", name)
    name = re.sub(r"(\(主\)|（主）|主)$", "", name)
    name = re.sub(r"^[\[\(（【]\s*", "", name)
    name = re.sub(r"\s*[\]\)）】]$", "", name)
    name = " ".join(name.split())
    if name in PLACEHOLDER_NAMES:
        return ""
    return name


def _extract_candidates(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    candidates = []

    def add_pair(h, a, score, source):
        h, a = _clean_team(h.strip() if h else ""), _clean_team(a.strip() if a else "")
        if not h or not a or h == a or h in PLACEHOLDER_NAMES or a in PLACEHOLDER_NAMES: 
            return
        candidates.append({"home": h, "away": a, "score": score, "source":  source})

    scripts_text = " ".join(s.get_text(" ", strip=True) for s in soup.find_all("script"))
    m1 = re.search(r"homeTeamName\s*[: =]\s*['\"]([^'\"]+)['\"]", scripts_text, re.IGNORECASE)
    m2 = re.search(r"(guestTeamName|awayTeamName|team2)\s*[:=]\s*['\"]([^'\"]+)['\"]", scripts_text, re.IGNORECASE)
    m1b = re.search(r"(home|host|team1)\s*[:=]\s*['\"]([^'\"]+)['\"]", scripts_text, re.IGNORECASE)
    m2b = re.search(r"(away|visitor|team2)\s*[:=]\s*['\"]([^'\"]+)['\"]", scripts_text, re.IGNORECASE)
    if m1 and m2:
        add_pair(m1.group(1), m2.group(2), 0.9, "script_named")
    if m1b and m2b: 
        add_pair(m1b.group(2), m2b.group(2), 0.75, "script_generic")

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.get_text())
            if isinstance(data, dict):
                h = data.get("homeTeam", {}).get("name") if isinstance(data.get("homeTeam"), dict) else data.get("homeTeam")
                a = data.get("awayTeam", {}).get("name") if isinstance(data.get("awayTeam"), dict) else data.get("awayTeam")
                add_pair(h or "", a or "", 0.8, "ld_json")
        except Exception:
            continue

    def meta(name):
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        return tag["content"].strip() if tag and tag.has_attr("content") else ""

    title = soup.title.get_text(strip=True) if soup.title else ""
    for cand in [meta("og:title"), meta("twitter:title"), title]:
        if not cand or cand.strip() in PLACEHOLDER_NAMES:
            continue
        m = re.search(r"([A-Za-z0-9\u4e00-\u9fff·\.\-]{2,})\s*V[Ss]\s*([A-Za-z0-9\u4e00-\u9fff·\.\-]{2,})", cand)
        if m:
            add_pair(m.group(1), m.group(2), 0.6, "meta/title")

    for htag in soup.find_all(["h1", "h2"]):
        txt = htag.get_text(" ", strip=True)
        if not txt: 
            continue
        m = re.search(r"([A-Za-z0-9\u4e00-\u9fff·\.\-]{2,})\s*V[Ss]\s*([A-Za-z0-9\u4e00-\u9fff·\.\-]{2,})", txt)
        if m:
            add_pair(m.group(1), m.group(2), 0.55, "h1/h2")

    selector_pairs = [
        ("#team1, .teamA, .home, .hometeam, [id*=homeTeamName], [id*=team1]", "#team2, .teamB, .away, .awayteam, [id*=guestTeamName], [id*=team2]"),
        ("[class*='home']", "[class*='away']"),
        (".msl-ls-home", ".msl-ls-away"),
        (".msl-ls-team-home", ".msl-ls-team-away"),
        (".team-home", ".team-away"),
        ("[class*='team'][class*='home']", "[class*='team'][class*='away']"),
        (".analyhead .home a, .analyhead.new .home a", ".analyhead .guest a, .analyhead.new .guest a"),
    ]
    for h_sel, a_sel in selector_pairs:
        h_el = soup.select_one(h_sel)
        a_el = soup.select_one(a_sel)
        if h_el and a_el:
            add_pair(h_el.get_text(strip=True), a_el.get_text(strip=True), 0.7, f"selector:{h_sel}|{a_sel}")

    text_full = soup.get_text(" ", strip=True)
    header_names = re.findall(r"\[[^\]]*\]\s*([A-Za-z0-9\u4e00-\u9fff·\.\-]+)", text_full)
    uniq = []
    for n in header_names:
        n = n.strip()
        if n and n not in uniq:
            uniq.append(n)
    if len(uniq) >= 2:
        add_pair(uniq[0], uniq[1], 0.65, "section_header_bracket")

    m_vs = re.search(r"([A-Za-z0-9\u4e00-\u9fff·\.\-]{2,})\s*V[Ss]\s*([A-Za-z0-9\u4e00-\u9fff·\.\-]{2,})", text_full)
    if m_vs:
        add_pair(m_vs.group(1), m_vs.group(2), 0.5, "text_vs")

    dedup = {}
    for c in candidates:
        key = (c["home"], c["away"])
        if key not in dedup or c["score"] > dedup[key]["score"]:
            dedup[key] = c
    return sorted(dedup.values(), key=lambda x: x["score"], reverse=True)


def load_aliases():
    try:
        return json.loads(Path(ALIAS_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {"teams": {}}


aliases = load_aliases()


def persist_aliases():
    Path(ALIAS_FILE).write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")


def add_alias_entry(name: str, source: str):
    if not name or name in PLACEHOLDER_NAMES:
        return
    teams = aliases.setdefault("teams", {})
    entry = teams.setdefault(name, {"variants": [], "sources": {}})
    if name not in entry["variants"]:
        entry["variants"].append(name)
    srcs = entry["sources"].setdefault(source, [])
    if name not in srcs:
        srcs.append(name)


async def titan_scrape_stats(match_id: str):
    url = f"https://zq.titan007.com/analysis/{match_id}.htm"
    out:  Dict[str, Any] = {
        "match_id": match_id,
        "url": url,
        "scraped_at": datetime.now().isoformat(),
        "sections_found": [],
        "sections_missing": [],
        "sections_debug": {},
        "sections_data": {},
        "has_stats": False,
        "home_team": "",
        "away_team": "",
        "game_time": "",
        "league": "",
        "team_candidates": [],
        "standard":  {},
    }

    html = None
    found = missing = debug = tables = []
    candidates:  List[Dict[str, Any]] = []
    soup = None

    async with async_playwright() as p:
        for attempt in range(2):
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
                await asyncio.sleep(2.5 + attempt * 2)
                html = await page.content()
            except Exception as e:
                out["error"] = str(e)
                await browser.close()
                return out
            await browser.close()

            found, missing, debug, tables = analyze_sections(html)
            soup = BeautifulSoup(html, "html.parser")
            candidates = _extract_candidates(soup)
            if found or candidates:
                break

    out["sections_found"] = found
    out["sections_missing"] = missing
    out["sections_debug"] = debug
    out["sections_data"] = tables
    out["has_stats"] = len(found) >= MIN_SECTIONS_FOR_FULL

    out["team_candidates"] = candidates[: 5]

    if soup is not None:
        ln = soup.select_one(".analyhead .vs .row a.LName, .analyhead.new .vs .row a.LName")
        if ln:
            out["league"] = clean_league(ln.get_text(strip=True))

    if candidates:
        best = None
        for c in candidates:
            if c["home"] and c["away"] and c["home"] not in PLACEHOLDER_NAMES and c["away"] not in PLACEHOLDER_NAMES:
                best = c
                break
        if best is None:
            best = candidates[0]
        out["home_team"] = best["home"]
        out["away_team"] = best["away"]

        add_alias_entry(out["home_team"], source="titan")
        add_alias_entry(out["away_team"], source="titan")
        if out.get("league"):
            add_alias_entry(out["league"], source="titan")
        try:
            persist_aliases()
        except Exception: 
            pass

    if not out.get("game_time") and soup is not None:
        t = soup.find(string=re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}"))
        if t:
            m = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}", t.replace("\u00a0", " "))
            if m:
                out["game_time"] = m.group(0)

    alias_map = load_alias_map()
    out["home_team_canon"] = alias_match(out.get("home_team", ""), alias_map)
    out["away_team_canon"] = alias_match(out.get("away_team", ""), alias_map)

    if not out.get("home_team") or not out.get("away_team"):
        out["has_stats"] = False

    kickoff_dt = None
    if out.get("game_time"):
        try:
            kickoff_dt = datetime.fromisoformat(out["game_time"].replace(" ", "T"))
        except Exception:
            kickoff_dt = None
    kickoff_date = kickoff_dt.date().isoformat() if kickoff_dt else ""
    kickoff_time = kickoff_dt.time().strftime("%H:%M") if kickoff_dt else ""

    out["standard"] = {
        "source": "titan",
        "event_id": out.get("match_id", ""),
        "home_team_raw": out.get("home_team", ""),
        "away_team_raw": out.get("away_team", ""),
        "home_team_canon": out.get("home_team_canon", ""),
        "away_team_canon": out.get("away_team_canon", ""),
        "kickoff":  out.get("game_time", "") or "",
        "kickoff_date":  kickoff_date,
        "kickoff_time": kickoff_time,
    }

    return out


def write_report_excel(excel_path: Path, full_rows: List[List[str]], missing_rows: List[List[str]]):
    headers = ["match_id", "home_team", "away_team", "game_time", "league", "stat_url", "found_sections", "missing_sections"]
    wb = openpyxl.Workbook()
    ws_full = wb.active
    ws_full.title = "captured"
    ws_full.append(headers)
    for row in full_rows:
        ws_full.append(row)

    ws_miss = wb.create_sheet("missing")
    ws_miss.append(headers)
    for row in missing_rows:
        ws_miss.append(row)

    wb.save(excel_path)


async def discover_match_ids(limit: Optional[int], min_id: int) -> List[str]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(INDEX_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(4.0)
        ids = await page.evaluate(
            """
            () => {
              const out = new Set();
              const globals = ['A','B','C','arr','arrData','Match','matchList'];
              for (const k of globals) {
                const v = window[k];
                if (Array.isArray(v)) {
                  for (const row of v) {
                    const cand = Array.isArray(row)
                      ? row[0]
                      : (row && (row.matchid || row.MatchID || row.matchId || row.id));
                    if (cand) out.add(String(cand));
                  }
                }
              }
              document.querySelectorAll('a[href*="analysis/"][href$=".htm"]').forEach(a => {
                const m = a.href.match(/analysis\\/(\d+)\\.htm/);
                if (m) out.add(m[1]);
              });
              return Array.from(out);
            }
            """
        )
        await browser.close()

    uniq = []
    seen = set()
    for mid in ids:
        if not (mid and mid.isdigit()):
            continue
        if int(mid) < min_id:
            continue
        if mid not in seen:
            seen.add(mid)
            uniq.append(mid)

    if limit: 
        uniq = uniq[:limit]
    logger.info("Discovered %d match ids from live page (limit=%s, min_id=%s)", len(uniq), limit, min_id)
    return uniq


# =============================================================================
# PHASE-BASED PROCESSING
# =============================================================================
def filter_matches_to_scrape(
    match_ids: List[str],
    registry: TitanRegistry,
    game_time_lookup: Dict[str, str],
    rescrape_all: bool = False,
    skip_started: bool = True,
) -> Tuple[List[str], Dict[str, str]]:
    """
    Phase 2:  Filter matches to determine which need scraping. 
    Returns (to_scrape_ids, skip_reasons).
    """
    logger.info("=" * 60)
    logger.info("PHASE 2: FILTER - Checking which matches need scraping")
    logger.info("=" * 60)

    to_scrape:  List[str] = []
    skip_reasons: Dict[str, str] = {}

    for mid in match_ids:
        # Check 1: Already in registry? 
        if not rescrape_all and registry.is_scraped(mid):
            skip_reasons[mid] = "already_scraped"
            continue

        # Check 2: Match already started?
        game_time = game_time_lookup.get(mid, "")
        if skip_started and game_time and should_skip_started(game_time):
            skip_reasons[mid] = "already_started"
            continue

        to_scrape.append(mid)

    # Summary
    total = len(match_ids)
    skipped_registry = sum(1 for r in skip_reasons.values() if r == "already_scraped")
    skipped_started = sum(1 for r in skip_reasons.values() if r == "already_started")
    need_scrape = len(to_scrape)

    logger.info(f"  Total matches discovered: {total}")
    logger.info(f"  ⏭ Skip (already scraped): {skipped_registry}")
    logger.info(f"  ⏭ Skip (already started): {skipped_started}")
    logger.info(f"  ✅ Need to scrape: {need_scrape}")

    return to_scrape, skip_reasons


async def main():
    parser = argparse.ArgumentParser(description="Titan Stats Scraper with Skip Optimization")
    parser.add_argument("--limit", type=int, help="Limit how many IDs from discovery")
    parser.add_argument("--base", default="titan/stats", help="Base stats directory")
    parser.add_argument("--min-id", type=int, default=1_000_000, help="Ignore match IDs below this value")
    parser.add_argument("--id", help="Scrape only this match_id (skip discovery/filtering)")
    parser.add_argument("--rescrape-all", action="store_true", help="Ignore registry and scrape everything")
    parser.add_argument("--no-skip-started", action="store_true", help="Don't skip matches that have already started")
    parser.add_argument("--registry-path", help="Custom path for scraped-matches registry JSON")
    args = parser.parse_args()

    base = Path(args.base)
    full_dir = base / "full"
    missing_dir = base / "missing"
    full_dir.mkdir(parents=True, exist_ok=True)
    missing_dir.mkdir(parents=True, exist_ok=True)

    # Initialize registry
    registry_path = Path(args.registry_path) if args.registry_path else base / "scraped_registry.json"
    registry = TitanRegistry(registry_path)
    logger.info(f"Loaded registry with {len(registry)} previously scraped matches")

    print("=" * 70)
    print("TITAN STATS SCRAPER - With Skip Optimization")
    print("=" * 70)
    print("Phase 1: Discover matches from live page")
    print("Phase 2: Filter - check registry & started matches")
    print("Phase 3: Scrape only new matches")
    print()

    # Single ID mode
    if args.id:
        print(f"Single match mode: {args.id}")
        out = await titan_scrape_stats(args.id)
        target_dir = full_dir if out.get("has_stats") else missing_dir
        (target_dir / f"{args.id}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        registry.mark_scraped(args.id)
        registry.save()
        status = "FULL" if out.get("has_stats") else "MISSING"
        print(f"✅ {status}:  {args.id} saved to {target_dir}")
        return

    # ==========================================================================
    # PHASE 1: DISCOVERY
    # ==========================================================================
    logger.info("=" * 60)
    logger.info("PHASE 1: DISCOVERY - Finding matches from live page")
    logger.info("=" * 60)

    ids = await discover_match_ids(args.limit, args.min_id)
    ids, league_lookup, game_time_lookup = await filter_match_ids_only_my_leagues(ids)
    logger.info(f"Filtered to {len(ids)} matches from allowed leagues")

    if not ids:
        logger.warning("No matches found!")
        return

    # ==========================================================================
    # PHASE 2: FILTER
    # ==========================================================================
    ids_to_scrape, skip_reasons = filter_matches_to_scrape(
        ids,
        registry,
        game_time_lookup,
        rescrape_all=args.rescrape_all,
        skip_started=not args.no_skip_started,
    )

    if not ids_to_scrape:
        logger.info("=" * 60)
        logger.info("✅ ALL MATCHES ALREADY PROCESSED - Nothing to scrape!")
        logger.info("=" * 60)
        return

    # ==========================================================================
    # PHASE 3: SCRAPE
    # ==========================================================================
    logger.info("=" * 60)
    logger.info("PHASE 3: SCRAPING - Collecting stats for new matches")
    logger.info("=" * 60)

    leagues_to_scrape = sorted({league_lookup.get(mid, "") for mid in ids_to_scrape if league_lookup.get(mid)})
    full_rows = []
    missing_rows = []
    cur_time_tag = datetime.now().strftime("%Y%m%d%H%M")
    reports_dir = Path("/home/andy/aitest/reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    excel_path = reports_dir / f"missingreport{cur_time_tag}.xlsx"

    total = len(ids_to_scrape)
    print(f"Leagues to scrape ({len(leagues_to_scrape)}): {leagues_to_scrape}")
    print(f"Ready to process {total} matches")

    for idx, mid in enumerate(ids_to_scrape, start=1):
        league_name = league_lookup.get(mid, "")
        game_time = game_time_lookup.get(mid, "")
        print(f"[{idx}/{total}] Scraping {mid} | League: {league_name or 'unknown'} | Time: {game_time or 'unknown'}...")

        out = await titan_scrape_stats(mid)

        if league_name:
            out["league"] = clean_league(league_name)
        if game_time and not out.get("game_time"):
            out["game_time"] = game_time

        row = [
            out["match_id"],
            out.get("home_team", ""),
            out.get("away_team", ""),
            out.get("game_time", ""),
            league_name or out.get("league", ""),
            out.get("url"),
            ";".join(out.get("sections_found") or []),
            ";".join(out.get("sections_missing") or [])
        ]

        target_dir = full_dir if out.get("has_stats") else missing_dir
        (target_dir / f"{mid}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Mark as scraped in registry
        registry.mark_scraped(mid)

        if out.get("has_stats"):
            full_rows.append(row)
            print(f"[{idx}/{total}] ✅ FULL: {mid} | {out.get('home_team')} vs {out.get('away_team')}")
        else:
            missing_rows.append(row)
            print(f"[{idx}/{total}] ⚠️ MISSING: {mid} | Sections: {len(out.get('sections_found', []))}")

    # Save registry after all scraping
    registry.save()

    # Write Excel report
    write_report_excel(excel_path, full_rows, missing_rows)

    # Summary
    logger.info("=" * 60)
    logger.info("SCRAPING COMPLETE")
    logger.info("=" * 60)
    logger.info(f"  Total processed: {total}")
    logger.info(f"  Full stats:  {len(full_rows)}")
    logger.info(f"  Missing stats: {len(missing_rows)}")
    logger.info(f"  Registry size: {len(registry)}")
    logger.info(f"  Excel report: {excel_path}")

    print(f"\n✅ Completed! Full:  {len(full_rows)}, Missing: {len(missing_rows)}")
    print(f"📊 Report:  {excel_path}")


if __name__ == "__main__":
    asyncio.run(main())
