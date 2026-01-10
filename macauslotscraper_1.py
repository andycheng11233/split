#!/usr/bin/env python3
"""
Fixed Market Data Collector with:
- PRE-SCAN OPTIMIZATION: Discover all events first, then scrape only new ones
- Persistent skip registry (can be bypassed with --rescrape-all)
- Per-event JSON outputs under macauslot/odds/runs/<timestamp>/events/
- Full snapshot + CSV remain under macauslot/odds/
- Deterministic pagination (inputs only, capped at 15)
- Baseline metadata reuse to avoid Unknown/synthetic IDs across markets
- Cross-midnight handling for date rollover
- Skips started matches when date+time are known
- Uses AliasHelper for automatic team/league management
"""

import asyncio
import json
import csv
import argparse
import logging
import sys
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

# Use unified alias helper
from alias_helper import AliasHelper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


def _norm_key(val: str) -> str:
    return re.sub(r"\s+", "", val.lower()) if val else ""


class FixedMarketsCollector:
    def __init__(
        self,
        headless: bool = True,
        output_dir: str = "macauslot",
        rescrape_all: bool = False,
        registry_path: Optional[str] = None,
    ):
        self.headless = headless
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.odds_dir = self.output_dir / "odds"
        self.odds_dir.mkdir(parents=True, exist_ok=True)

        self.rescrape_all = rescrape_all
        self.registry_path = Path(registry_path) if registry_path else self.odds_dir / "scraped_events_registry.json"
        self.scraped_registry: Set[str] = self._load_scraped_registry()

        self.allowed_markets = {
            "全場三合一賠率", "上半場三合一賠率", "上半場角球數賠率",
            "上半場波膽", "上半場波膽組合", "上半場入球單/雙數",
            "上半場球隊入球數", "角球數賠率", "波膽", "波膽組合",
            "上半場/全場賽果", "入球單/雙數", "全場入球總數",
            "上/下半場入球較多", "球隊入球數", "最先入球球隊", "首名入球球員",
        }
        self.expected_counts = {
            "全場三合一賠率": 19,
            "上半場三合一賠率": 7,
            "上半場角球數賠率": 7,
            "上半場波膽": 26,
            "上半場波膽組合": 6,
            "上半場入球單/雙數": 2,
            "上半場球隊入球數": 12,
            "角球數賠率": 7,
            "波膽": 26,
            "波膽組合": 6,
            "上半場/全場賽果": 9,
            "入球單/雙數": 2,
            "全場入球總數": 4,
            "上/下半場入球較多": 3,
            "球隊入球數": 12,
            "最先入球球隊": 3,
            "首名入球球員": 23,
        }
        # Use AliasHelper instead of loading map directly
        self.alias_helper = AliasHelper()

    def _load_scraped_registry(self) -> Set[str]:
        p = self.registry_path
        if not p.exists():
            return set()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return set(data if isinstance(data, list) else [])
        except Exception:
            return set()

    def _save_scraped_registry(self):
        try:
            self.registry_path.write_text(
                json.dumps(sorted(self.scraped_registry), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"Registry saved: {self.registry_path}")
        except Exception as e:
            logger.warning(f"Failed to save registry: {e}")

    def _build_event_key(self, event_id: str, home: str, away: str, match_date: str, match_time: str) -> str:
        if event_id and event_id != "unknown" and not event_id.startswith("全場") and not event_id.startswith("上半場"):
            return f"id:{event_id}"
        return f"comp:{_norm_key(home)}|{_norm_key(away)}|{match_date or ''}|{match_time or ''}"

    def _should_skip_started(self, match_time: str, match_date: Optional[str]) -> bool:
        if not match_time or match_time == "Unknown":
            return False
        try:
            hh, mm = map(int, match_time.split(":"))
        except Exception:
            return False

        now = datetime.now()
        if match_date:
            try:
                dt_date = datetime.strptime(match_date, "%Y-%m-%d").date()
            except Exception:
                dt_date = None
        else:
            dt_date = None

        if dt_date:
            candidate = datetime.combine(dt_date, datetime.min.time()).replace(hour=hh, minute=mm)
            if candidate.date() < now.date():
                return True
            if candidate.date() == now.date() and (now - candidate) > timedelta(minutes=5):
                return True
            return False
        return False

    async def _discover_all_events(self, page) -> Dict[str, Dict]:
        logger.info("=" * 60)
        logger.info("PHASE 1: DISCOVERY - Scanning all events")
        logger.info("=" * 60)

        all_events: Dict[str, Dict] = {}
        market_names = await self._get_market_tabs(page)

        if not market_names:
            logger.error("No market tabs found!")
            return {}

        logger.info(f"Found {len(market_names)} market tabs")

        discovery_market = None
        for m in market_names:
            if m in self.allowed_markets:
                discovery_market = m
                break

        if not discovery_market:
            discovery_market = market_names[0] if market_names else None

        if not discovery_market:
            logger.error("No markets available for discovery!")
            return {}

        if not await self._click_market_button(page, discovery_market):
            logger.error(f"Failed to click market: {discovery_market}")
            return {}

        await asyncio.sleep(3)
        await page.wait_for_load_state("networkidle")

        page_numbers = await self._get_real_page_numbers(page)
        if not page_numbers:
            page_numbers = [1]

        total_discovered = 0

        for page_num in page_numbers:
            if page_num > 1:
                if not await self._click_page_number(page, page_num):
                    break
                await asyncio.sleep(3)
                await page.wait_for_load_state("networkidle")

            page_date_iso = await self._extract_page_date(page)
            current_matches = await self._get_current_page_matches(page)

            if not current_matches:
                break

            headers = []
            times = []
            for match_el in current_matches:
                home_team, away_team, event_id, match_time = await self._extract_match_header(match_el)
                minutes = None
                if match_time != "Unknown":
                    try:
                        hh, mm = map(int, match_time.split(":"))
                        minutes = hh * 60 + mm
                    except Exception:
                        minutes = None
                headers.append({
                    "home": home_team,
                    "away": away_team,
                    "event_id": event_id,
                    "match_time": match_time,
                    "minutes": minutes,
                })
                times.append(minutes)

            initial_offset = 0
            if page_date_iso and times:
                first = times[0]
                has_late = any(t is not None and t >= 720 for t in times)
                if first is not None and first < 360 and has_late:
                    initial_offset = 1

            offset = initial_offset
            working_date = None
            if page_date_iso:
                try:
                    dt = datetime.strptime(page_date_iso, "%Y-%m-%d") + timedelta(days=offset)
                    working_date = dt.strftime("%Y-%m-%d")
                except Exception:
                    working_date = page_date_iso

            prev_minutes = None

            for match_idx, h in enumerate(headers):
                home_team = h["home"]
                away_team = h["away"]
                event_id = h["event_id"]
                match_time = h["match_time"]
                minutes = h["minutes"]

                if working_date and minutes is not None and prev_minutes is not None:
                    if minutes + 60 < prev_minutes:
                        try:
                            dt = datetime.strptime(working_date, "%Y-%m-%d") + timedelta(days=1)
                            working_date = dt.strftime("%Y-%m-%d")
                            offset += 1
                        except Exception:
                            pass

                if minutes is not None:
                    prev_minutes = minutes

                if event_id == "unknown":
                    event_id = f"discovered_page{page_num}_match{match_idx + 1}"

                event_key = self._build_event_key(event_id, home_team, away_team, working_date or "", match_time)

                if event_key not in all_events:
                    all_events[event_key] = {
                        "event_id": event_id,
                        "home_team": home_team,
                        "away_team": away_team,
                        "match_date": working_date or "",
                        "match_time": match_time,
                        "markets_found_in": [discovery_market],
                        "page_positions": {discovery_market: (page_num, match_idx)},
                    }
                    total_discovered += 1

        logger.info(f"Discovery complete: found {total_discovered} unique events")
        return all_events

    def _filter_events_to_scrape(self, all_events: Dict[str, Dict]) -> Tuple[Dict[str, Dict], Dict[str, str]]:
        logger.info("=" * 60)
        logger.info("PHASE 2: FILTER - Checking which events need scraping")
        logger.info("=" * 60)

        to_scrape: Dict[str, Dict] = {}
        skip_reasons: Dict[str, str] = {}

        for event_key, event_data in all_events.items():
            home = event_data["home_team"]
            away = event_data["away_team"]
            match_date = event_data["match_date"]
            match_time = event_data["match_time"]

            if not self.rescrape_all and event_key in self.scraped_registry:
                skip_reasons[event_key] = "already_scraped"
                continue

            if self._should_skip_started(match_time, match_date):
                skip_reasons[event_key] = "already_started"
                continue

            to_scrape[event_key] = event_data

        total = len(all_events)
        skipped_registry = sum(1 for r in skip_reasons.values() if r == "already_scraped")
        skipped_started = sum(1 for r in skip_reasons.values() if r == "already_started")
        need_scrape = len(to_scrape)

        logger.info(f"  Total events discovered: {total}")
        logger.info(f"  Skip (already scraped): {skipped_registry}")
        logger.info(f"  Skip (already started): {skipped_started}")
        logger.info(f"  Need to scrape: {need_scrape}")

        return to_scrape, skip_reasons

    async def _targeted_scrape(self, page, to_scrape: Dict[str, Dict]) -> Dict[str, Dict]:
        logger.info("=" * 60)
        logger.info("PHASE 3: TARGETED SCRAPE - Collecting odds for new events")
        logger.info("=" * 60)

        if not to_scrape:
            logger.info("Nothing to scrape - all events already processed!")
            return {}

        all_events_data: Dict[str, Dict] = {}
        market_names = await self._get_market_tabs(page)

        to_scrape_keys = set(to_scrape.keys())

        position_to_key: Dict[Tuple[int, int], str] = {}
        for event_key, event_data in to_scrape.items():
            for market, pos in event_data.get("page_positions", {}).items():
                position_to_key[pos] = event_key

        market_idx = 0
        baseline = {}

        for market_name in market_names:
            if market_name not in self.allowed_markets:
                continue

            if not await self._click_market_button(page, market_name):
                continue

            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle")

            page_numbers = await self._get_real_page_numbers(page)
            if not page_numbers:
                page_numbers = [1]

            for page_num in page_numbers:
                if page_num > 1:
                    if not await self._click_page_number(page, page_num):
                        break
                    await asyncio.sleep(3)
                    await page.wait_for_load_state("networkidle")

                page_date_iso = await self._extract_page_date(page)
                current_matches = await self._get_current_page_matches(page)

                if not current_matches:
                    break

                headers = []
                times = []
                for match_el in current_matches:
                    home_team, away_team, event_id, match_time = await self._extract_match_header(match_el)
                    minutes = None
                    if match_time != "Unknown":
                        try:
                            hh, mm = map(int, match_time.split(":"))
                            minutes = hh * 60 + mm
                        except Exception:
                            minutes = None
                    headers.append({
                        "el": match_el,
                        "home": home_team,
                        "away": away_team,
                        "event_id": event_id,
                        "match_time": match_time,
                        "minutes": minutes,
                    })
                    times.append(minutes)

                initial_offset = 0
                if page_date_iso and times:
                    first = times[0]
                    has_late = any(t is not None and t >= 720 for t in times)
                    if first is not None and first < 360 and has_late:
                        initial_offset = 1

                offset = initial_offset
                working_date = None
                if page_date_iso:
                    try:
                        dt = datetime.strptime(page_date_iso, "%Y-%m-%d") + timedelta(days=offset)
                        working_date = dt.strftime("%Y-%m-%d")
                    except Exception:
                        working_date = page_date_iso

                prev_minutes = None

                for match_idx, h in enumerate(headers):
                    match_el = h["el"]
                    home_team = h["home"]
                    away_team = h["away"]
                    event_id = h["event_id"]
                    match_time = h["match_time"]
                    minutes = h["minutes"]

                    key = (page_num, match_idx)
                    if key in baseline:
                        if event_id == "unknown":
                            event_id = baseline[key]["event_id"]
                        if home_team == "Unknown":
                            home_team = baseline[key]["home_team"]
                        if away_team == "Unknown":
                            away_team = baseline[key]["away_team"]
                        if match_time == "Unknown":
                            match_time = baseline[key]["match_time"]
                        if not working_date and baseline[key].get("match_date"):
                            working_date = baseline[key]["match_date"]

                    if working_date and minutes is not None and prev_minutes is not None:
                        if minutes + 60 < prev_minutes:
                            try:
                                dt = datetime.strptime(working_date, "%Y-%m-%d") + timedelta(days=1)
                                working_date = dt.strftime("%Y-%m-%d")
                                offset += 1
                            except Exception:
                                pass

                    if minutes is not None:
                        prev_minutes = minutes

                    if event_id == "unknown":
                        event_id = f"{market_name}_page{page_num}_match{match_idx + 1}"

                    event_key = self._build_event_key(event_id, home_team, away_team, working_date or "", match_time)

                    needs_scrape = event_key in to_scrape_keys or key in position_to_key

                    if not needs_scrape:
                        continue

                    print(f"  {home_team} vs {away_team} | {working_date or 'Unknown'} {match_time}")

                    try:
                        market_data = await self._extract_market_data(match_el, market_name, market_idx)

                        if event_id not in all_events_data:
                            all_events_data[event_id] = {
                                "event_id": event_id,
                                "home_team": home_team,
                                "away_team": away_team,
                                "match_time": match_time,
                                "match_date": working_date or "",
                                "scrape_time": datetime.now().isoformat(),
                                "markets": {}
                            }

                        all_events_data[event_id]["markets"][market_name] = market_data

                        baseline[key] = {
                            "event_id": event_id,
                            "home_team": home_team,
                            "away_team": away_team,
                            "match_time": match_time,
                            "match_date": working_date or "",
                        }

                        self.scraped_registry.add(event_key)

                    except Exception as e:
                        if event_id not in all_events_data:
                            all_events_data[event_id] = {
                                "event_id": event_id,
                                "home_team": home_team,
                                "away_team": away_team,
                                "match_time": match_time,
                                "match_date": working_date or "",
                                "scrape_time": datetime.now().isoformat(),
                                "markets": {}
                            }

                        all_events_data[event_id]["markets"][market_name] = {
                            "market_name": market_name,
                            "error": str(e),
                            "numbers_count": 0,
                            "all_numbers": [],
                            "timestamp": datetime.now().isoformat()
                        }

            market_idx += 1

        return all_events_data

    async def collect_all(self):
        all_events_data = {}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless, slow_mo=200)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await context.new_page()

            try:
                logger.info("Loading Macau Slot website...")
                await page.goto(
                    "https://www.macau-slot.com/content/soccer/coming_bet.html",
                    timeout=60000,
                    wait_until="networkidle"
                )

                await asyncio.sleep(3)
                await self._set_page_size(page)
                await asyncio.sleep(2)

                discovered_events = await self._discover_all_events(page)

                if not discovered_events:
                    await browser.close()
                    return {}

                to_scrape, skip_reasons = self._filter_events_to_scrape(discovered_events)

                if not to_scrape:
                    await browser.close()
                    return {}

                all_events_data = await self._targeted_scrape(page, to_scrape)

                if all_events_data:
                    await self._save_all_events(all_events_data)
                    logger.info(f"Successfully collected data for {len(all_events_data)} matches")

            except Exception as e:
                logger.critical(f"Critical error: {e}", exc_info=True)
            finally:
                await browser.close()

        return all_events_data

    async def _get_real_page_numbers(self, page) -> List[int]:
        try:
            max_candidate = 1
            inputs = await page.query_selector_all("input")
            for inp in inputs:
                for attr in ("data-max", "max", "data-page", "value"):
                    v = await inp.get_attribute(attr)
                    if v and v.isdigit():
                        num = int(v)
                        if 1 <= num <= 15:
                            max_candidate = max(max_candidate, num)
            pages = list(range(1, max_candidate + 1))
            return pages if pages else [1]
        except Exception:
            return [1]

    async def _click_page_number(self, page, page_num: int) -> bool:
        try:
            prev = await page.content()
            inp = await page.query_selector(f"input[value='{page_num}']")
            if inp:
                await inp.scroll_into_view_if_needed()
                await inp.click(force=True)
                await page.wait_for_timeout(400)
                await page.wait_for_load_state("networkidle")
                new = await page.content()
                return new != prev
            return False
        except Exception:
            return False

    async def _set_page_size(self, page):
        selectors = [
            'select[name*="per"]',
            'select:has(option[value="50"])'
        ]
        for selector in selectors:
            try:
                sel = await page.query_selector(selector)
                if not sel:
                    continue
                await sel.select_option("50")
                await page.wait_for_timeout(1500)
                await page.wait_for_load_state("networkidle")
                return True
            except Exception:
                continue
        return False

    async def _get_market_tabs(self, page) -> List[str]:
        buttons = await page.query_selector_all("li.msl-cm-methods")
        market_names = []
        for btn in buttons:
            try:
                text = await btn.text_content()
                if text and text.strip():
                    market_names.append(text.strip())
            except:
                continue
        return market_names

    async def _get_current_page_matches(self, page) -> List:
        selectors = [".msl-ls-item", ".match-row", "tr[data-ev-id]", "div[data-ev-id]"]
        for selector in selectors:
            matches = await page.query_selector_all(selector)
            if matches:
                return matches
        return []

    async def _click_market_button(self, page, market_name: str) -> bool:
        try:
            await page.click(f"text='{market_name}'", timeout=3000)
            await asyncio.sleep(2)
            return True
        except:
            try:
                elements = await page.query_selector_all("li.msl-cm-methods")
                for elem in elements:
                    text = await elem.text_content()
                    if text and text.strip() == market_name:
                        await elem.click()
                        await asyncio.sleep(2)
                        return True
            except:
                pass
        return False

    async def _extract_match_header(self, match_el):
        try:
            event_id = (
                await match_el.get_attribute("data-ev-id")
                or await match_el.get_attribute("data-event-id")
                or "unknown"
            )

            selector_pairs = [
                ("[class*='home']", "[class*='away']"),
                (".msl-ls-home", ".msl-ls-away"),
            ]
            home, away = None, None
            for h_sel, a_sel in selector_pairs:
                h_el = await match_el.query_selector(h_sel)
                a_el = await match_el.query_selector(a_sel)
                if h_el and a_el:
                    h_txt = (await h_el.text_content() or "").strip()
                    a_txt = (await a_el.text_content() or "").strip()
                    if h_txt and a_txt:
                        home, away = h_txt, a_txt
                        break

            text = (await match_el.text_content() or "").strip()
            lines = [l.strip() for l in text.split('\n') if l.strip()]

            match_time = self._extract_match_time(lines)

            home = home or "Unknown"
            away = away or "Unknown"
            return home, away, event_id, match_time
        except:
            return "Unknown", "Unknown", "unknown", "Unknown"

    def _extract_match_time(self, lines: List[str]) -> str:
        for line in lines:
            if ':' in line and 4 <= len(line) <= 5:
                try:
                    h, m = line.split(':')
                    if h.isdigit() and m.isdigit():
                        hh, mm = int(h), int(m)
                        if 0 <= hh < 24 and 0 <= mm < 60:
                            return f"{hh:02d}:{mm:02d}"
                except:
                    continue
        return "Unknown"

    async def _extract_page_date(self, page) -> Optional[str]:
        try:
            txt = await page.evaluate("() => document.body.innerText") or ""
            m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", txt)
            if m:
                y, mo, d = m.groups()
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except Exception:
            return None
        return None

    async def _extract_market_data(self, match_el, market_name: str, index: int) -> Dict:
        try:
            text = await match_el.text_content()
            lines = [l.strip() for l in text.split('\n') if l.strip()]
            numbers = self._extract_all_numbers(lines)

            return {
                "market_name": market_name,
                "market_index": index,
                "numbers_count": len(numbers),
                "all_numbers": numbers,
                "all_lines": lines,
                "total_lines": len(lines),
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "market_name": market_name,
                "market_index": index,
                "error": str(e),
                "numbers_count": 0,
                "all_numbers": [],
                "timestamp": datetime.now().isoformat()
            }

    def _extract_all_numbers(self, lines: List[str]) -> List[float]:
        numbers = []
        for line in lines:
            parts = line.replace('\t', ' ').replace('|', ' ').split(' ')
            for part in parts:
                clean = part.replace(',', '').replace('—', '-').strip()
                if self._is_number(clean):
                    try:
                        num = float(clean)
                        if 0.5 <= num <= 1000:
                            numbers.append(num)
                    except:
                        continue
        return numbers

    def _is_number(self, text: str) -> bool:
        if not text:
            return False
        if text.startswith('-'):
            text = text[1:]
        return text.replace('.', '', 1).isdigit()

    async def _save_all_events(self, all_events_data: Dict):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        json_file = self.odds_dir / f"market_data_complete_{timestamp}.json"

        run_dir = self.odds_dir / "runs" / timestamp
        events_dir = run_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)

        complete_data = {
            "scrape_time": datetime.now().isoformat(),
            "total_events": len(all_events_data),
            "events": all_events_data
        }

        # Inject canon names using AliasHelper
        for eid, ev in complete_data["events"].items():
            home = ev.get("home_team", "")
            away = ev.get("away_team", "")
            
            # Add new teams to alias.json
            if home and home != "Unknown":
                self.alias_helper.add_team_if_new(home, source="macauslot")
            if away and away != "Unknown":
                self.alias_helper.add_team_if_new(away, source="macauslot")
            
            # Get canonical names
            ev["home_team_canon"] = self.alias_helper.match_team(home)
            ev["away_team_canon"] = self.alias_helper.match_team(away)
            
            ev["standard"] = {
                "source": "macauslot",
                "event_id": eid,
                "home_team_raw": home,
                "away_team_raw": away,
                "home_team_canon": ev["home_team_canon"],
                "away_team_canon": ev["away_team_canon"],
                "kickoff": ((ev.get("match_date") or "") + (" " + ev.get("match_time") if ev.get("match_time") not in (None, "", "Unknown") else "")).strip(),
                "kickoff_date": ev.get("match_date", "") or "",
                "kickoff_time": ev.get("match_time", "") if ev.get("match_time") != "Unknown" else "",
            }

        # Save alias changes
        self.alias_helper.save()

        json_file.write_text(
            json.dumps(complete_data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        logger.info(f"JSON saved: {json_file.name}")

        index_rows = []
        for eid, ev in complete_data["events"].items():
            out_path = events_dir / f"{eid}.json"
            out_path.write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
            index_rows.append({
                "event_id": eid,
                "home": ev.get("home_team", ""),
                "away": ev.get("away_team", ""),
                "kickoff": ev.get("standard", {}).get("kickoff", ""),
                "path": str(out_path.relative_to(self.output_dir))
            })
        (run_dir / "index.json").write_text(json.dumps(index_rows, indent=2, ensure_ascii=False), encoding="utf-8")

        csv_file = self.odds_dir / f"market_summary_{timestamp}.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                "EventID", "Home", "Away", "MatchDate", "MatchTime", "Market",
                "NumbersCount", "HasError"
            ])

            for event_id, event_data in complete_data["events"].items():
                for market_name, market_data in event_data.get("markets", {}).items():
                    has_error = "error" in market_data
                    writer.writerow([
                        event_id,
                        event_data.get("home_team", ""),
                        event_data.get("away_team", ""),
                        event_data.get("match_date", ""),
                        event_data.get("match_time", ""),
                        market_name,
                        market_data.get("numbers_count", 0),
                        "YES" if has_error else "NO",
                    ])

        logger.info(f"CSV saved: {csv_file.name}")

        # Print alias stats
        alias_stats = self.alias_helper.get_stats()
        logger.info(f"New alias entries this session: {alias_stats['new_entries_this_session']}")

        self._save_scraped_registry()
        return json_file


async def main():
    parser = argparse.ArgumentParser(description="Fixed Market Data Collector with AliasHelper")
    parser.add_argument("--visible", action="store_true", help="Run with visible browser")
    parser.add_argument("--output", default="macauslot", help="Base output directory")
    parser.add_argument("--registry-path", help="Custom path for scraped-events registry JSON")
    parser.add_argument("--rescrape-all", action="store_true", help="Ignore registry and scrape everything")
    parser.add_argument("--id", help="Save single event to market_data_single_<id>.json")
    args = parser.parse_args()

    print("=" * 70)
    print("FIXED MARKET DATA COLLECTOR - With AliasHelper")
    print("=" * 70)

    collector = FixedMarketsCollector(
        headless=not args.visible,
        output_dir=args.output,
        rescrape_all=args.rescrape_all,
        registry_path=args.registry_path,
    )

    all_events = await collector.collect_all()

    if args.id:
        evt = all_events.get(str(args.id)) if isinstance(all_events, dict) else None
        out_dir = Path(args.output)
        if evt:
            single_path = out_dir / f"market_data_single_{args.id}.json"
            single_path.write_text(json.dumps(evt, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Saved single event to {single_path}")
        else:
            print(f"Event {args.id} not found in fetched data")


if __name__ == "__main__":
    asyncio.run(main())
