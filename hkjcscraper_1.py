#!/usr/bin/env python3
"""
HKJC standalone odds scraper with skip rules and detailed metrics.
Updated version using AliasHelper for automatic team/league management.

Skip rules (default enabled):
1) Skip scraping if there is an existing odds JSON for the event with non-empty markets.
2) Skip saving if the parsed kickoff time has already passed (game started).

Run:
  python hkjcscraper_1.py --id 50059564          # single event
  python hkjcscraper_1.py                        # bulk (home -> allodds)
  python hkjcscraper_1.py --max-events 20        # bulk limited
Options:
  --no-skip-existing      Disable skip rule #1
  --no-skip-started       Disable skip rule #2
"""
import argparse
import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# Use unified alias helper
from alias_helper import AliasHelper

# --- logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("hkjc")


# --- util ---
def cprint(msg: str):
    print(msg)


# --- parsers for all-odds page ---
AO_DISPLAY_ZH = {
    "HAD": "主客和", "FHA": "半場主客和", "HHA": "讓球主客和", "HHA_Extra": "讓球主客和",
    "HDC": "讓球", "HIL": "入球大細", "FHL": "半場入球大細",
    "CHL": "開出角球大細", "FCH": "半場開出角球大細",
    "CHD": "開出角球讓球", "FHC": "半場開出角球讓球",
    "CRS": "波膽", "FCS": "半場波膽", "FTS": "第一隊入球",
    "TTG": "總入球", "OOE": "入球單雙", "HFT": "半全場",
    "FGS": "首名入球", "LGS": "最後入球球員", "AGS": "任何時間入球球員",
    "MSP": "特別項目",
}


def ao_clean_text(el):
    return el.get_text(strip=True) if el else None


def ao_clean_odds_text(span):
    if not span:
        return None
    text = span.get_text(strip=True)
    cleaned = re.sub(r"[^\d.]", "", text)
    return float(cleaned) if cleaned else None


def ao_next_match_row_container(coupon):
    if not coupon:
        return None
    sib = coupon.find_next_sibling()
    while sib is not None and not ("match-row-container" in sib.get("class", [])):
        sib = sib.find_next_sibling()
    return sib


def ao_parse_allodds_match_header(soup):
    mi = soup.select_one(".match-info")
    if not mi:
        return {}
    match_id = ao_clean_text(mi.select_one(".match .val"))
    home = ao_clean_text(mi.select_one(".team .home"))
    away = ao_clean_text(mi.select_one(".team .away"))
    time_raw = ao_clean_text(mi.select_one(".time .val"))
    timg = mi.select_one(".matchInfoTourn img")
    tournament = timg["title"] if timg and timg.has_attr("title") else None
    return {
        "match_id": match_id,
        "home_team": home,
        "away_team": away,
        "tournament": tournament,
        "time_raw": time_raw,
    }


def ao_parse_had_like_from_row(row, odds_class):
    odds_block = row.select_one(f".odds.{odds_class}")
    if not odds_block:
        return None
    grids = odds_block.select(".oddsCheckboxGrid")
    if len(grids) < 3:
        return None
    co = ao_clean_odds_text
    return {
        "home_odds": co(grids[0].select_one(".add-to-slip")),
        "draw_odds": co(grids[1].select_one(".add-to-slip")),
        "away_odds": co(grids[2].select_one(".add-to-slip")),
    }


def ao_parse_hha_multi_lines_from_row(row):
    res = []
    odds_line = row.select_one(".oddsLine.HHA")
    if not odds_line:
        return res
    line_blocks = odds_line.select(".odds.show")
    for line_index, line_block in enumerate(line_blocks):
        items = line_block.select(".hdcOddsItem")
        if len(items) != 3:
            continue

        def cond(item):
            c = item.select_one(".cond")
            return ao_clean_text(c).strip("[]") if c else ""

        home_hcap = cond(items[0]); draw_hcap = cond(items[1]); away_hcap = cond(items[2])
        co = ao_clean_odds_text
        home_odds = co(items[0].select_one(".add-to-slip"))
        draw_odds = co(items[1].select_one(".add-to-slip"))
        away_odds = co(items[2].select_one(".add-to-slip"))
        give_home = None
        if home_hcap.startswith("-"):
            give_home = True
        elif away_hcap.startswith("-"):
            give_home = False
        else:
            if home_odds is not None and away_odds is not None:
                give_home = home_odds < away_odds
        market_type = "HHA" if line_index == 0 else "HHA_Extra"
        res.append({
            "market_type": market_type,
            "line_index": line_index + 1,
            "home_odds": home_odds, "draw_odds": draw_odds, "away_odds": away_odds,
            "euro_handicap_value": home_hcap,
            "euro_handicap_give_home": give_home,
        })
    return res


def ao_parse_hdc_from_row(row):
    odds_line = row.select_one(".oddsLine.HDC")
    if not odds_line:
        return None
    lb = odds_line.select_one(".odds.show")
    if not lb:
        return None
    items = lb.select(".hdcOddsItem")
    if len(items) != 2:
        return None

    def cond(item):
        c = item.select_one(".cond")
        return ao_clean_text(c).strip("[]") if c else ""

    home_hcap = cond(items[0]); away_hcap = cond(items[1])
    co = ao_clean_odds_text
    home_odds = co(items[0].select_one(".add-to-slip"))
    away_odds = co(items[1].select_one(".add-to-slip"))
    give_home = None
    if home_hcap.startswith("-"):
        give_home = True
    elif away_hcap.startswith("-"):
        give_home = False
    else:
        if home_odds is not None and away_odds is not None:
            give_home = home_odds < away_odds
    return {
        "asia_handicap_value": home_hcap,
        "asia_handicap_give_home": give_home,
        "home_odds": home_odds, "away_odds": away_odds,
    }


def ao_parse_ou_market_from_row(row, class_name, goal_field_name="goal_line"):
    odds_line = row.select_one(f".oddsLine.{class_name}")
    if not odds_line:
        return []
    res = []
    line_nums = odds_line.select(".lineNum.show")
    odds_blocks = odds_line.select(".odds.show")
    for line_idx, (ln, ob) in enumerate(zip(line_nums, odds_blocks)):
        line_text = ao_clean_text(ln).strip("[]") if ln else None
        grids = ob.select(".oddsCheckboxGrid")
        if len(grids) < 2:
            continue
        co = ao_clean_odds_text
        over_odds = co(grids[0].select_one(".add-to-slip"))
        under_odds = co(grids[1].select_one(".add-to-slip"))
        res.append({
            "line_index": line_idx + 1,
            goal_field_name: line_text,
            "over_odds": over_odds,
            "under_odds": under_odds,
        })
    return res


def ao_parse_crs_matrix(row):
    res = []
    for odds_cell in row.select(".crsTable .odds"):
        score = ao_clean_text(odds_cell.select_one(".crsSel"))
        odds = ao_clean_odds_text(odds_cell.select_one(".add-to-slip"))
        if score and odds is not None:
            res.append({"score": score, "odds": odds})
    return res


def ao_parse_fts(row):
    odds_block = row.select_one(".oddsFTS")
    if not odds_block:
        return None
    grids = odds_block.select(".oddsCheckboxGrid")
    if len(grids) < 3:
        return None
    co = ao_clean_odds_text
    return {
        "home_first": co(grids[0].select_one(".add-to-slip")),
        "no_goal": co(grids[1].select_one(".add-to-slip")),
        "away_first": co(grids[2].select_one(".add-to-slip")),
    }


def ao_parse_ttg(row):
    odds_block = row.select_one(".oddsTTG")
    if not odds_block:
        return []
    res = []
    for block in odds_block.find_all("div", recursive=False):
        goals = ao_clean_text(block.select_one(".goals-number"))
        odds = ao_clean_odds_text(block.select_one(".add-to-slip"))
        if goals and odds is not None:
            res.append({"goals": goals, "odds": odds})
    return res


def ao_parse_ooe(row):
    odds_block = row.select_one(".oddsOOE")
    if not odds_block:
        return None
    grids = odds_block.select(".oddsCheckboxGrid")
    if len(grids) < 2:
        return None
    co = ao_clean_odds_text
    return {
        "odd": co(grids[0].select_one(".add-to-slip")),
        "even": co(grids[1].select_one(".add-to-slip")),
    }


def ao_parse_hft(row):
    odds_block = row.select_one(".oddsHFT")
    if not odds_block:
        return []
    res = []
    for block in odds_block.find_all("div", recursive=False):
        label = ao_clean_text(block.select_one(".goals-number"))
        odds = ao_clean_odds_text(block.select_one(".add-to-slip"))
        if label and odds is not None:
            res.append({"combo": label, "odds": odds})
    return res


def ao_parse_scorer_market(row):
    res = []
    grids = row.select(".oddsCheckboxGrid")

    def pull_candidates(grid):
        cands = []
        for sib in grid.previous_siblings:
            if getattr(sib, "get_text", None):
                cands.append(ao_clean_text(sib))
        parent = grid.find_parent(["td", "div"])
        if parent:
            for sib in parent.find_previous_siblings():
                if getattr(sib, "get_text", None):
                    cands.append(ao_clean_text(sib))
        for prev in grid.find_all_previous(["div", "td", "th", "span"], limit=8):
            txt = ao_clean_text(prev)
            if txt:
                cands.append(txt)
        return cands

    for g in grids:
        odds = ao_clean_odds_text(g.find_next("span", class_="add-to-slip"))
        gid = g.get("id", "") or ""
        m = re.search(r"_(\d{3,})", gid)
        code = m.group(1) if m else None
        name = None
        for txt in pull_candidates(g):
            if not txt:
                continue
            m2 = re.search(r"\b(\d{3})\b\s*([A-Za-z\u4e00-\u9fff].+)", txt)
            if m2:
                if not code:
                    code = m2.group(1)
                name = m2.group(2).strip()
                break
        res.append({"player_code": code, "player_name": name, "odds": odds})
    return res


def ao_parse_msp(row):
    raw = row.get_text(" ", strip=True)
    if not raw:
        return []
    items = []
    parts = re.split(r"項目編號[:：]\s*", raw)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m_id = re.match(r"(\d+)", part)
        item_id = m_id.group(1) if m_id else None
        m_q = re.split(r"\(\d+\)", part, maxsplit=1)
        if len(m_q) == 2:
            question = m_q[0].strip()
            rest = "(" + m_q[1]
        else:
            question = None
            rest = part
        options = []
        for opt_num, label, odds in re.findall(r"\((\d+)\)\s*([^(]+?)\s+(\d+(?:\.\d+)?)", rest):
            options.append({"option": opt_num, "label": label.strip(), "odds": float(odds)})
        items.append({"item_id": item_id, "question": question, "options": options, "raw": part})
    if not items:
        odds_list = [ao_clean_odds_text(span) for span in row.select(".add-to-slip")]
        items.append({"raw": raw, "odds": [o for o in odds_list if o is not None]})
    return items


def ao_parse_allodds_from_html(html: str):
    soup = BeautifulSoup(html, "html.parser")
    match_meta = ao_parse_allodds_match_header(soup)
    markets = {}

    def add_display(code, obj):
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj["display_name_zh"] = AO_DISPLAY_ZH.get(code, "")
        return obj

    def add_display_list(code, lst):
        if lst is None:
            return None
        return [item | {"display_name_zh": AO_DISPLAY_ZH.get(code, "")} for item in lst]

    for code, cls, parser in [
        ("HAD", "couponHAD", lambda r: ao_parse_had_like_from_row(r, "oddsHAD")),
        ("FHA", "couponFHA", lambda r: ao_parse_had_like_from_row(r, "oddsFHA")),
        ("HDC", "couponHDC", ao_parse_hdc_from_row),
        ("CHD", "couponCHD", ao_parse_hdc_from_row),
        ("FHC", "couponFHC", ao_parse_hdc_from_row),
    ]:
        coupon = soup.select_one(f".coupon.{cls}")
        if coupon:
            row = ao_next_match_row_container(coupon)
            if row:
                markets[code] = add_display(code, parser(row))

    coupon = soup.select_one(".coupon.couponHHA")
    if coupon:
        row = ao_next_match_row_container(coupon)
        if row:
            hha_lines = ao_parse_hha_multi_lines_from_row(row)
            for line in hha_lines:
                line["display_name_zh"] = AO_DISPLAY_ZH.get(line["market_type"], "")
                markets.setdefault(line["market_type"], []).append(line)

    for code, cls in [("HIL", "couponHIL"), ("FHL", "couponFHL"), ("CHL", "couponCHL"), ("FCH", "couponFCH")]:
        coupon = soup.select_one(f".coupon.{cls}")
        if coupon:
            row = ao_next_match_row_container(coupon)
            if row:
                markets[code] = add_display_list(code, ao_parse_ou_market_from_row(row, code))

    for code, cls in [("CRS", "couponCRS"), ("FCS", "couponFCS")]:
        coupon = soup.select_one(f".coupon.{cls}")
        if coupon:
            row = ao_next_match_row_container(coupon)
            if row:
                markets[code] = {"display_name_zh": AO_DISPLAY_ZH[code], "scores": ao_parse_crs_matrix(row)}

    coupon = soup.select_one(".coupon.couponFTS")
    if coupon:
        row = ao_next_match_row_container(coupon)
        if row:
            markets["FTS"] = add_display("FTS", ao_parse_fts(row))
    coupon = soup.select_one(".coupon.couponTTG")
    if coupon:
        row = ao_next_match_row_container(coupon)
        if row:
            markets["TTG"] = {"display_name_zh": AO_DISPLAY_ZH["TTG"], "buckets": ao_parse_ttg(row)}
    coupon = soup.select_one(".coupon.couponOOE")
    if coupon:
        row = ao_next_match_row_container(coupon)
        if row:
            markets["OOE"] = add_display("OOE", ao_parse_ooe(row))
    coupon = soup.select_one(".coupon.couponHFT")
    if coupon:
        row = ao_next_match_row_container(coupon)
        if row:
            markets["HFT"] = {"display_name_zh": AO_DISPLAY_ZH["HFT"], "combos": ao_parse_hft(row)}

    for code, cls in [("FGS", "couponFGS"), ("LGS", "couponLGS"), ("AGS", "couponAGS")]:
        coupon = soup.select_one(f".coupon.{cls}")
        if coupon:
            row = ao_next_match_row_container(coupon)
            if row:
                markets[code] = {"display_name_zh": AO_DISPLAY_ZH[code], "players": ao_parse_scorer_market(row)}

    coupon = soup.select_one(".coupon.couponMSP")
    if coupon:
        row = ao_next_match_row_container(coupon)
        if row:
            markets["MSP"] = {"display_name_zh": AO_DISPLAY_ZH["MSP"], "items": ao_parse_msp(row)}

    return match_meta, markets


# --- HKJC detailed odds scraper ---
class HKJCDetailedOddsScraper:
    def __init__(self, output_dir: Path = Path("hkjc/odds"), skip_existing: bool = True, skip_started: bool = True):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Use AliasHelper instead of loading map directly
        self.alias_helper = AliasHelper()
        self.alias_map = self.alias_helper.alias_map
        self.skip_existing = skip_existing
        self.skip_started = skip_started

    def _existing_event_file(self, event_id: str) -> Path | None:
        """Return any existing odds file for the event_id with non-empty markets."""
        candidates = sorted(self.output_dir.glob(f"hkjc_odds_{event_id}_*.json"))
        for p in reversed(candidates):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                markets = data.get("markets") or {}
                if markets:
                    return p
            except Exception:
                continue
        return None

    @staticmethod
    def _parse_kickoff(time_raw: str) -> datetime | None:
        if not time_raw:
            return None
        for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y%H:%M"):
            try:
                return datetime.strptime(time_raw.strip(), fmt)
            except Exception:
                continue
        return None

    async def scrape(self, event_id: str) -> dict:
        if self.skip_existing:
            existing = self._existing_event_file(event_id)
            if existing:
                logger.info("Skip %s (existing odds file with markets: %s)", event_id, existing.name)
                return {"status": "skipped_existing", "event_id": event_id, "data": None}

        url = f"https://bet.hkjc.com/ch/football/allodds/{event_id}"
        logger.info("Scraping HKJC all odds for event %s", event_id)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/118.0.5993.117 Safari/537.36"
                )
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                try:
                    await page.wait_for_selector(".match-info", timeout=30000)
                except Exception:
                    logger.warning(".match-info not found for %s", event_id)
                html = await page.content()
            except Exception as e:
                logger.error("Error scraping detailed odds %s: %s", event_id, e)
                await browser.close()
                return {"status": "failed", "event_id": event_id, "data": None}
            await browser.close()

        try:
            match_meta, markets = ao_parse_allodds_from_html(html)
            home = (match_meta or {}).get("home_team") or ""
            away = (match_meta or {}).get("away_team") or ""
            tournament = (match_meta or {}).get("tournament") or ""
            
            # ===== NEW: Add teams/leagues to alias.json =====
            if home:
                self.alias_helper.add_team_if_new(home, source="hkjc", league=tournament)
            if away:
                self.alias_helper.add_team_if_new(away, source="hkjc", league=tournament)
            if tournament:
                self.alias_helper.add_league_if_new(tournament, source="hkjc")
            # Save changes to alias.json
            self.alias_helper.save()
            # ===== END NEW =====
            
            home_canon = self.alias_helper.match_team(home)
            away_canon = self.alias_helper.match_team(away)

            kickoff_raw = (match_meta or {}).get("time_raw") or ""
            kickoff_dt = self._parse_kickoff(kickoff_raw)
            if self.skip_started and kickoff_dt and datetime.now() >= kickoff_dt:
                logger.info("Skip %s (kickoff in the past: %s)", event_id, kickoff_raw)
                return {"status": "skipped_started", "event_id": event_id, "data": None}

            out_data = {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "event_id": event_id,
                "url": url,
                "match": match_meta,
                "markets": markets,
                "home_team_canon": home_canon,
                "away_team_canon": away_canon,
                "standard": {
                    "source": "hkjc",
                    "event_id": event_id,
                    "home_team_raw": home,
                    "away_team_raw": away,
                    "home_team_canon": home_canon,
                    "away_team_canon": away_canon,
                    "kickoff": kickoff_raw,
                    "kickoff_date": "",
                    "kickoff_time": kickoff_raw,
                },
            }
            out_path = self.output_dir / f"hkjc_odds_{event_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            out_path.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"status": "success", "event_id": event_id, "data": out_data}
        except Exception as e:
            logger.error("Error parsing detailed odds %s: %s", event_id, e)
            return {"status": "failed", "event_id": event_id, "data": None}


# --- HKJC home scraper (collect event IDs without per-row navigation) ---
class HKJCHomeScraper:
    BET_HOME = "https://bet.hkjc.com/ch/football/home"
    ROWS_SEL = ".match-row,.event-row"
    DT_FORMAT = "%d/%m/%Y %H:%M"
    CLICK_WAIT = 4.0
    TIMEOUT_MS = 9000
    HEADLESS = True

    async def safe_goto(self, page, url: str, max_attempts: int = 3) -> None:
        for attempt in range(1, max_attempts + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                return
            except Exception as e:
                if attempt == max_attempts:
                    raise
                logger.warning("Goto attempt %d failed (%s); retrying...", attempt, e)
                await asyncio.sleep(2 * attempt)

    async def get_declared(self, page):
        html = await page.content()
        m = re.search(r"共有\s*(\d+)\s*場賽事", html)
        return int(m.group(1)) if m else None

    async def scroll_bottom(self, page):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1.0)

    async def click_show_more(self, page) -> bool:
        selectors = [
            "xpath=//*[contains(text(), '顯示更多')]",
            "xpath=//*[contains(text(), '更多')]",
            "text=顯示更多",
            "button:has-text('顯示更多')",
        ]
        for sel in selectors:
            els = await page.query_selector_all(sel)
            for el in els:
                try:
                    if await el.is_visible():
                        await el.click(force=True)
                        logger.info("Clicked '顯示更多'")
                        return True
                except Exception:
                    continue
        return False

    async def ensure_rows_loaded(self, page, declared: int | None, max_clicks: int = 15, max_rounds: int = 25) -> int:
        total = await page.locator(self.ROWS_SEL).count()
        logger.info("Initial rows loaded: %d (declared=%s)", total, declared or "unknown")

        clicks = 0
        stagnant_rounds = 0
        rounds = 0

        while True:
            rounds += 1
            if declared and total >= declared:
                return total
            if rounds > max_rounds:
                logger.info("Stop loading rows: hit max_rounds=%d", max_rounds)
                return total

            await self.scroll_bottom(page)
            clicked = await self.click_show_more(page)
            if clicked:
                clicks += 1
                await page.wait_for_timeout(int(self.CLICK_WAIT * 1000))
            else:
                stagnant_rounds += 1
                await page.wait_for_timeout(800)

            new_total = await page.locator(self.ROWS_SEL).count()
            logger.info("Rows after load attempt %d: %d", rounds, new_total)

            if new_total > total:
                total = new_total
                stagnant_rounds = 0

            if (declared and total >= declared) or clicks >= max_clicks or stagnant_rounds >= 3:
                if clicks >= max_clicks:
                    logger.info("Stop loading rows: reached max_clicks=%d", max_clicks)
                if stagnant_rounds >= 3:
                    logger.info("Stop loading rows: stagnant for %d rounds", stagnant_rounds)
                return total

    async def extract_event_ids_from_dom(self, page) -> set[str]:
        js = """
        (() => {
          const rows = Array.from(document.querySelectorAll('.match-row,.event-row'));
          const ids = new Set();
          const grabNums = (text) => {
            if (!text) return;
            const re = /(allodds\\/(\\d{6,})|\\b(\\d{7,})\\b)/g;
            let m;
            while ((m = re.exec(text)) !== null) {
              const id = m[2] || m[3];
              if (id) ids.add(id);
            }
          };
          for (const row of rows) {
            grabNums(row.innerHTML);
            grabNums(row.textContent);
            const attrs = ['href','data-href','data-url','onclick','id'];
            for (const attr of attrs) {
              const v = row.getAttribute(attr);
              grabNums(v);
            }
            const links = row.querySelectorAll('a,button,div,span');
            for (const el of links) {
              const attrs2 = ['href','data-href','data-url','onclick','id'];
              for (const attr of attrs2) {
                const v = el.getAttribute && el.getAttribute(attr);
                grabNums(v);
              }
            }
          }
          return Array.from(ids);
        })();
        """
        try:
            data = await page.evaluate(js)
            return set(data or [])
        except Exception as e:
            logger.warning("extract_event_ids_from_dom failed: %s", e)
            return set()

    async def scrape(self):
        results: list[dict] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.HEADLESS,
                args=["--disable-blink-features=AutomationControlled"]
            )
            ctx = await browser.new_context()
            page = await ctx.new_page()
            page.set_default_timeout(self.TIMEOUT_MS)

            await self.safe_goto(page, self.BET_HOME, max_attempts=3)
            await asyncio.sleep(4.0)

            declared = await self.get_declared(page)
            logger.info("Declared matches: %s", declared or "unknown")

            await self.ensure_rows_loaded(page, declared)
            ids = await self.extract_event_ids_from_dom(page)
            logger.info("Extracted %d event ids from DOM", len(ids))

            if declared and len(ids) < declared:
                await self.ensure_rows_loaded(page, declared)
                more_ids = await self.extract_event_ids_from_dom(page)
                ids |= more_ids
                logger.info("After retry, have %d event ids (declared=%s)", len(ids), declared)

            ids_sorted = sorted(ids, key=int)
            for eid in ids_sorted:
                results.append({"event_id": eid, "home_team": None, "away_team": None, "raw_text": ""})

            await browser.close()

        return results, declared, len(ids_sorted)


# --- Bulk collector: home -> detailed odds with concurrency ---
class HKJCBulkOddsCollector:
    def __init__(self, home_scraper: HKJCHomeScraper, detailed_scraper: HKJCDetailedOddsScraper):
        self.home_scraper = home_scraper
        self.detailed_scraper = detailed_scraper

    async def collect(self, max_events: int | None = None, concurrency: int = 4):
        home_rows, declared_count, extracted_count = await self.home_scraper.scrape()
        event_ids = [r["event_id"] for r in home_rows if r.get("event_id")]
        if max_events is not None:
            event_ids = event_ids[:max_events]

        sem = asyncio.Semaphore(concurrency)
        results: dict[str, dict] = {}
        failed: list[str] = []

        metrics = {
            "processed_success": 0,
            "failed": 0,
            "skipped_existing": 0,
            "skipped_started": 0,
            "declared_events": declared_count,
            "extracted_event_ids": extracted_count,
        }

        logger.info("Detailed scraping starting with max concurrency: %s", concurrency)

        async def worker(eid: str):
            async with sem:
                try:
                    res = await self.detailed_scraper.scrape(eid)
                    status = res.get("status")
                    if status == "success" and res.get("data"):
                        results[eid] = res["data"]
                        metrics["processed_success"] += 1
                    elif status == "skipped_existing":
                        metrics["skipped_existing"] += 1
                    elif status == "skipped_started":
                        metrics["skipped_started"] += 1
                    else:
                        metrics["failed"] += 1
                        failed.append(eid)
                except Exception as e:
                    logger.warning("Failed for %s: %s", eid, e)
                    metrics["failed"] += 1
                    failed.append(eid)

        await asyncio.gather(*(worker(eid) for eid in event_ids))
        out_dir = self.detailed_scraper.output_dir
        out_path = out_dir / f"hkjc_allodds_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        payload = {
            "metadata": {
                "scraped_at": datetime.now().isoformat(),
                "source": "HKJC",
                "declared_events": metrics["declared_events"],
                "extracted_event_ids": metrics["extracted_event_ids"],
                "total_event_ids": len(event_ids),
                "processed_success": metrics["processed_success"],
                "failed": failed,
                "failed_count": metrics["failed"],
                "skipped_existing": metrics["skipped_existing"],
                "skipped_started": metrics["skipped_started"],
            },
            "events": list(results.values())
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info(
            "Scraping complete! Declared events: %s, Extracted: %s, Processed successfully: %d, "
            "Failed: %d, Skipped (existing): %d, Skipped (started): %d",
            metrics["declared_events"] if metrics["declared_events"] is not None else "unknown",
            metrics["extracted_event_ids"],
            metrics["processed_success"],
            metrics["failed"],
            metrics["skipped_existing"],
            metrics["skipped_started"],
        )
        logger.info("Saved HKJC all odds to: %s (events=%d)", out_path, len(results))
        return results, home_rows, metrics


# --- CLI entrypoint ---
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", help="Scrape single event_id (skip home crawl)")
    parser.add_argument("--max-events", type=int, default=None, help="Limit number of events in bulk mode")
    parser.add_argument("--concurrency", type=int, default=5, help="Concurrency for detailed odds in bulk mode")
    parser.add_argument("--no-skip-existing", action="store_true", help="Disable skipping when an odds file already exists")
    parser.add_argument("--no-skip-started", action="store_true", help="Disable skipping when kickoff time is in the past")
    args = parser.parse_args()

    cprint("🚀 HKJC standalone odds scraper (with AliasHelper)")
    detailed_scraper = HKJCDetailedOddsScraper(
        skip_existing=not args.no_skip_existing,
        skip_started=not args.no_skip_started,
    )

    if args.id:
        res = await detailed_scraper.scrape(args.id)
        status = res.get("status")
        if status == "success":
            cprint(f"✅ Detailed odds collected for event {args.id}")
        elif status == "skipped_existing":
            cprint(f"ℹ️ Skipped (existing odds file) for event {args.id}")
        elif status == "skipped_started":
            cprint(f"ℹ️ Skipped (kickoff in the past) for event {args.id}")
        else:
            cprint(f"ℹ️ Skipped or failed for event {args.id}")
        return

    home_scraper = HKJCHomeScraper()
    collector = HKJCBulkOddsCollector(home_scraper, detailed_scraper)
    results, home_rows, metrics = await collector.collect(max_events=args.max_events, concurrency=args.concurrency)
    cprint(f"✅ Detailed odds collected: {len(results)} events")
    cprint(f"ℹ️ Home rows captured: {len(home_rows)}")
    cprint(
        f"ℹ️ Summary -> Declared: {metrics['declared_events']}, Extracted: {metrics['extracted_event_ids']}, "
        f"Processed: {metrics['processed_success']}, Failed: {metrics['failed']}, "
        f"Skipped(existing): {metrics['skipped_existing']}, Skipped(started): {metrics['skipped_started']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
