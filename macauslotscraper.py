#!/usr/bin/env python3
"""
Fixed Market Data Collector with: 
- PRE-SCAN OPTIMIZATION:  Discover all events first, then scrape only new ones
- Persistent skip registry (can be bypassed with --rescrape-all)
- Per-event JSON outputs under macauslot/odds/runs/<timestamp>/events/
- Full snapshot + CSV remain under macauslot/odds/
- Deterministic pagination (inputs only, capped at 15)
- Baseline metadata reuse to avoid Unknown/synthetic IDs across markets
- Cross-midnight handling for date rollover
- Skips started matches when date+time are known
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

ALIAS_FILE = "alias.json"


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
        self.scraped_registry:  Set[str] = self._load_scraped_registry()

        self.allowed_markets = {
            "全場三合一賠率", "上半場三合一賠率", "上半場角球數賠率",
            "上半場波膽", "上半場波膽組合", "上半場入球單/雙數",
            "上半場球隊入球數", "角球數賠率", "波膽", "波膽組合",
            "上半場/全場賽果", "入球單/雙數", "全場入球總數",
            "上/下半場入球較多", "球隊入球數", "最先入球球隊", "首名入球球員",
        }
        self.expected_counts = {
            "全場三合一賠率":  19,
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
        self.alias_map = load_alias_map()

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
            logger.warning(f"Failed to save registry:  {e}")

    def _build_event_key(self, event_id: str, home:  str, away: str, match_date: str, match_time: str) -> str:
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

    # =========================================================================
    # PHASE 1: DISCOVERY - Fast scan to collect all event metadata
    # =========================================================================
    async def _discover_all_events(self, page) -> Dict[str, Dict]: 
        """
        Phase 1: Discover all events across all markets/pages. 
        Returns a dict keyed by event_key with metadata.
        """
        logger.info("=" * 60)
        logger.info("PHASE 1: DISCOVERY - Scanning all events")
        logger.info("=" * 60)

        all_events:  Dict[str, Dict] = {}
        market_names = await self._get_market_tabs(page)

        if not market_names:
            logger.error("No market tabs found!")
            return {}

        logger.info(f"Found {len(market_names)} market tabs")
        logger.info(f"Markets: {market_names}")

        # Use the first allowed market for discovery
        discovery_market = None
        for m in market_names:
            if m in self.allowed_markets:
                discovery_market = m
                break

        if not discovery_market:
            discovery_market = market_names[0] if market_names else None
            if discovery_market: 
                logger.warning(f"No allowed markets found, using first available: {discovery_market}")

        if not discovery_market:
            logger.error("No markets available for discovery!")
            return {}

        logger.info(f"Using '{discovery_market}' for event discovery")

        if not await self._click_market_button(page, discovery_market):
            logger.error(f"Failed to click market:  {discovery_market}")
            return {}

        await asyncio.sleep(3)
        await page.wait_for_load_state("networkidle")

        page_numbers = await self._get_real_page_numbers(page)
        if not page_numbers:
            page_numbers = [1]

        logger.info(f"Detected {len(page_numbers)} pages to scan")

        total_discovered = 0

        for page_num in page_numbers:
            if page_num > 1:
                logger.info(f"  📄 Scanning page {page_num}...")
                if not await self._click_page_number(page, page_num):
                    logger.warning(f"  Failed to navigate to page {page_num}, stopping discovery")
                    break
                await asyncio.sleep(3)
                await page.wait_for_load_state("networkidle")

            page_date_iso = await self._extract_page_date(page)
            current_matches = await self._get_current_page_matches(page)

            if not current_matches: 
                logger.warning(f"  No matches found on page {page_num}")
                break

            logger.info(f"  Page {page_num}:  found {len(current_matches)} matches (date: {page_date_iso or 'Unknown'})")

            # Extract headers and compute working dates
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
                    "home":  home_team,
                    "away": away_team,
                    "event_id": event_id,
                    "match_time": match_time,
                    "minutes": minutes,
                })
                times.append(minutes)

            # Cross-midnight handling
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

                # Cross-midnight rollover
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

                # Generate fallback event_id if needed
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

        logger.info(f"✅ Discovery complete: found {total_discovered} unique events")
        return all_events

    # =========================================================================
    # PHASE 2: FILTER - Determine which events need scraping
    # =========================================================================
    def _filter_events_to_scrape(self, all_events: Dict[str, Dict]) -> Tuple[Dict[str, Dict], Dict[str, str]]:
        """
        Phase 2: Filter events to determine which need scraping.
        """
        logger.info("=" * 60)
        logger.info("PHASE 2: FILTER - Checking which events need scraping")
        logger.info("=" * 60)

        to_scrape:  Dict[str, Dict] = {}
        skip_reasons: Dict[str, str] = {}

        for event_key, event_data in all_events.items():
            home = event_data["home_team"]
            away = event_data["away_team"]
            match_date = event_data["match_date"]
            match_time = event_data["match_time"]

            # Check 1: Already in registry? 
            if not self.rescrape_all and event_key in self.scraped_registry:
                skip_reasons[event_key] = "already_scraped"
                logger.debug(f"  ⏭ SKIP (already scraped): {home} vs {away}")
                continue

            # Check 2: Match already started?
            if self._should_skip_started(match_time, match_date):
                skip_reasons[event_key] = "already_started"
                logger.debug(f"  ⏭ SKIP (already started): {home} vs {away} | {match_date} {match_time}")
                continue

            # This event needs scraping
            to_scrape[event_key] = event_data

        # Summary
        total = len(all_events)
        skipped_registry = sum(1 for r in skip_reasons.values() if r == "already_scraped")
        skipped_started = sum(1 for r in skip_reasons.values() if r == "already_started")
        need_scrape = len(to_scrape)

        logger.info(f"  Total events discovered: {total}")
        logger.info(f"  ⏭ Skip (already scraped): {skipped_registry}")
        logger.info(f"  ⏭ Skip (already started): {skipped_started}")
        logger.info(f"  ✅ Need to scrape: {need_scrape}")

        return to_scrape, skip_reasons

    # =========================================================================
    # PHASE 3: TARGETED SCRAPE - Only scrape events that need it
    # =========================================================================
    async def _targeted_scrape(self, page, to_scrape: Dict[str, Dict]) -> Dict[str, Dict]:
        """
        Phase 3: Scrape only the events identified in Phase 2.
        """
        logger.info("=" * 60)
        logger.info("PHASE 3: TARGETED SCRAPE - Collecting odds for new events")
        logger.info("=" * 60)

        if not to_scrape:
            logger.info("✅ Nothing to scrape - all events already processed!")
            return {}

        all_events_data:  Dict[str, Dict] = {}
        market_names = await self._get_market_tabs(page)

        # Build a set of event_keys we need to scrape for quick lookup
        to_scrape_keys = set(to_scrape.keys())

        # Build position-based lookup:  (page_num, match_idx) -> event_key
        position_to_key:  Dict[Tuple[int, int], str] = {}
        for event_key, event_data in to_scrape.items():
            for market, pos in event_data.get("page_positions", {}).items():
                position_to_key[pos] = event_key

        market_idx = 0
        baseline = {}

        for market_name in market_names:
            if market_name not in self.allowed_markets:
                continue

            logger.info(f"\n{'=' * 60}")
            logger.info(f"Processing Market {market_idx + 1}/{len(self.allowed_markets)}: {market_name}")
            logger.info(f"{'=' * 60}")

            if not await self._click_market_button(page, market_name):
                logger.error(f"Failed to click market: {market_name}")
                continue

            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle")

            page_numbers = await self._get_real_page_numbers(page)
            if not page_numbers:
                page_numbers = [1]

            total_processed = 0
            total_skipped = 0

            for page_num in page_numbers: 
                if page_num > 1:
                    logger.info(f"  📄 Navigating to page {page_num}...")
                    if not await self._click_page_number(page, page_num):
                        logger.warning(f"  Failed to navigate to page {page_num}, stopping for this market")
                        break
                    await asyncio.sleep(3)
                    await page.wait_for_load_state("networkidle")

                page_date_iso = await self._extract_page_date(page)
                current_matches = await self._get_current_page_matches(page)

                if not current_matches:
                    logger.warning(f"  No matches found on page {page_num}")
                    break

                # Extract headers
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

                # Cross-midnight handling
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

                    # Baseline reuse
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

                    # Cross-midnight rollover
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

                    # Check if this event needs scraping
                    needs_scrape = event_key in to_scrape_keys or key in position_to_key

                    if not needs_scrape:
                        total_skipped += 1
                        continue

                    # This event needs scraping
                    total_processed += 1
                    print(f"  ⚽ {home_team} vs {away_team} | {working_date or 'Unknown'} {match_time}")

                    try:
                        market_data = await self._extract_market_data(match_el, market_name, market_idx)

                        if market_data.get("numbers_count", 0) > 0:
                            print(f"    ✅ {market_data['numbers_count']} odds")
                        else:
                            print(f"    ⚠️  0 odds")

                        if event_id not in all_events_data: 
                            all_events_data[event_id] = {
                                "event_id": event_id,
                                "home_team": home_team,
                                "away_team": away_team,
                                "match_time": match_time,
                                "match_date": working_date or "",
                                "scrape_time": datetime.now().isoformat(),
                                "markets":  {}
                            }

                        all_events_data[event_id]["markets"][market_name] = market_data

                        baseline[key] = {
                            "event_id": event_id,
                            "home_team": home_team,
                            "away_team": away_team,
                            "match_time": match_time,
                            "match_date": working_date or "",
                        }

                        # Add to registry
                        self.scraped_registry.add(event_key)

                    except Exception as e: 
                        error_msg = str(e)[:50]
                        print(f"    ❌ Error:  {error_msg}...")

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

            logger.info(f"  ✅ Completed {market_name} | processed={total_processed} skipped={total_skipped}")
            market_idx += 1

        return all_events_data

    # =========================================================================
    # MAIN ENTRY POINT
    # =========================================================================
    async def collect_all(self):
        all_events_data = {}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless, slow_mo=200)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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

                # ─────────────────────────────────────────────────────────────
                # PHASE 1: DISCOVERY
                # ─────────────────────────────────────────────────────────────
                discovered_events = await self._discover_all_events(page)

                if not discovered_events:
                    logger.warning("No events discovered!")
                    await browser.close()
                    return {}

                # ─────────────────────────────────────────────────────────────
                # PHASE 2: FILTER
                # ─────────────────────────────────────────────────────────────
                to_scrape, skip_reasons = self._filter_events_to_scrape(discovered_events)

                if not to_scrape:
                    logger.info("=" * 60)
                    logger.info("✅ ALL EVENTS ALREADY PROCESSED - Nothing to scrape!")
                    logger.info("=" * 60)
                    await browser.close()
                    return {}

                # ─────────────────────────────────────────────────────────────
                # PHASE 3: TARGETED SCRAPE
                # ─────────────────────────────────────────────────────────────
                all_events_data = await self._targeted_scrape(page, to_scrape)

                if all_events_data:
                    await self._save_all_events(all_events_data)
                    logger.info(f"\n✅ Successfully collected data for {len(all_events_data)} matches")
                else:
                    logger.warning("No data collected!")

            except Exception as e:
                logger.critical(f"Critical error: {e}", exc_info=True)
            finally:
                await browser.close()

        return all_events_data

    # =========================================================================
    # HELPER METHODS (from original working version)
    # =========================================================================
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
        except Exception as e:
            logger.warning(f"Error getting page numbers: {e}")
            return [1]

    async def _click_page_number(self, page, page_num:  int) -> bool:
        try:
            prev = await page.content()
            inp = await page.query_selector(f"input[value='{page_num}']")
            if not inp:
                all_inp = await page.query_selector_all("input")
                for cand in all_inp:
                    for attr in ("data-page", "data-max", "max"):
                        v = await cand.get_attribute(attr)
                        if v and v.isdigit() and int(v) == page_num:
                            inp = cand
                            break
                    if inp:
                        break

            if inp:
                await inp.scroll_into_view_if_needed()
                await inp.click(force=True)
                await page.wait_for_timeout(400)
                await page.wait_for_load_state("networkidle")
                new = await page.content()
                return new != prev

            btn = await page.query_selector(f"a:has-text('{page_num}'), button:has-text('{page_num}')")
            if btn:
                await btn.scroll_into_view_if_needed()
                await btn.click(force=True)
                await page.wait_for_timeout(400)
                await page.wait_for_load_state("networkidle")
                new = await page.content()
                return new != prev
            return False
        except Exception as e:
            logger.warning(f"Page click failed: {e}")
            return False

    async def _set_page_size(self, page):
        logger.info("Attempting to set page size to 50...")
        selectors = [
            'select[name*="per"]',
            'select[class*="perpage"]',
            'select[name*="record"]',
            'select[id*="per"]',
            'select:has(option[value="50"])'
        ]
        for selector in selectors:
            try: 
                sel = await page.query_selector(selector)
                if not sel:
                    continue
                await sel.select_option("50")
                await page.evaluate("(el) => el.dispatchEvent(new Event('change', {bubbles: true}))", sel)
                await page.wait_for_timeout(1500)
                await page.wait_for_load_state("networkidle")
                logger.info("Page size set to 50")
                return True
            except Exception: 
                continue
        logger.warning("Could not set page size to 50, using default")
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
        max_retries = 2
        for attempt in range(max_retries):
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
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
        logger.error(f"Failed to click market:  {market_name}")
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
                (".msl-ls-team-home", ".msl-ls-team-away"),
                (".team-home", ".team-away"),
                ("[class*='team'][class*='home']", "[class*='team'][class*='away']"),
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

            if (not home or not away) and lines:
                for i, line in enumerate(lines):
                    if line == '|' and i > 0 and i + 1 < len(lines):
                        home = home or lines[i - 1]
                        away = away or lines[i + 1]
                        break
                    if "|" in line:
                        parts = [p.strip() for p in line.split("|") if p.strip()]
                        if len(parts) == 2:
                            home = home or parts[0]
                            away = away or parts[1]
                            break
                if (not home or not away):
                    for line in lines[: 3]:
                        if "vs" in line.lower():
                            parts = line.lower().split("vs")
                            if len(parts) >= 2:
                                home = home or parts[0].strip()
                                away = away or parts[1].strip()
                                break

            match_time = self._extract_match_time(lines)

            home = home or "Unknown"
            away = away or "Unknown"
            return home, away, event_id, match_time
        except: 
            return "Unknown", "Unknown", "unknown", "Unknown"

    def _extract_match_time(self, lines:  List[str]) -> str:
        for line in lines:
            if ': ' in line and 4 <= len(line) <= 5:
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
            locator = page.locator(r"text=/\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日/").first
            if await locator.count() > 0:
                txt = await locator.inner_text()
            else:
                txt = await page.evaluate("() => document.body.innerText") or ""
            m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", txt)
            if m:
                y, mo, d = m.groups()
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except Exception:
            return None
        return None

    async def _extract_market_data(self, match_el, market_name:  str, index: int) -> Dict:
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
                "market_analysis": self._analyze_market_pattern(market_name, numbers),
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "market_name":  market_name,
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

    def _analyze_market_pattern(self, market_name: str, numbers: List[float]) -> Dict:
        n = len(numbers)
        if "波膽" in market_name and n == 26:
            pattern, description = "CORRECT_SCORE_26", "10 pairs (home/away) + 6 home-only"
        elif n == 3:
            pattern, description = "THREE_WAY", "1X2 (Home/Draw/Away)"
        elif n == 2:
            pattern, description = "BINARY", "Two-way market"
        elif n > 20:
            pattern, description = "GRID_COMPLEX", f"Complex grid with {n} numbers"
        else:
            pattern, description = "UNKNOWN", f"Unknown pattern with {n} numbers"

        stats = {}
        if numbers:
            stats = {
                "min": min(numbers),
                "max": max(numbers),
                "avg": sum(numbers) / n,
                "has_decimals": any(x != int(x) for x in numbers)
            }

        return {
            "pattern_type": pattern,
            "description": description,
            "number_count": n,
            "stats": stats
        }

    async def _save_all_events(self, all_events_data: Dict):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Full snapshot under odds dir
        json_file = self.odds_dir / f"market_data_complete_{timestamp}.json"

        # Per-run directories
        run_dir = self.odds_dir / "runs" / timestamp
        events_dir = run_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)

        complete_data = {
            "scrape_time": datetime.now().isoformat(),
            "total_events": len(all_events_data),
            "events":  all_events_data
        }

        # inject standard + canon
        for eid, ev in complete_data["events"].items():
            home = ev.get("home_team", "")
            away = ev.get("away_team", "")
            ev["home_team_canon"] = alias_match(home, self.alias_map)
            ev["away_team_canon"] = alias_match(away, self.alias_map)
            ev["standard"] = {
                "source": "macau",
                "event_id": eid,
                "home_team_raw": home,
                "away_team_raw": away,
                "home_team_canon": ev["home_team_canon"],
                "away_team_canon":  ev["away_team_canon"],
                "kickoff":  ((ev.get("match_date") or "") + (" " + ev.get("match_time") if ev.get("match_time") not in (None, "", "Unknown") else "")).strip(),
                "kickoff_date": ev.get("match_date", "") or "",
                "kickoff_time":  ev.get("match_time", "") if ev.get("match_time") != "Unknown" else "",
            }

        json_file.write_text(
            json.dumps(complete_data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        logger.info(f"JSON saved:  {json_file.name}")

        # Per-event files + index
        index_rows = []
        for eid, ev in complete_data["events"].items():
            out_path = events_dir / f"{eid}.json"
            out_path.write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
            index_rows.append({
                "event_id": eid,
                "home":  ev.get("home_team", ""),
                "away": ev.get("away_team", ""),
                "kickoff": ev.get("standard", {}).get("kickoff", ""),
                "path": str(out_path.relative_to(self.output_dir))
            })
        (run_dir / "index.json").write_text(json.dumps(index_rows, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Per-event files saved under {events_dir}")

        # CSV stays with odds dir
        csv_file = self.odds_dir / f"market_summary_{timestamp}.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                "EventID", "Home", "Away", "MatchDate", "MatchTime", "Market",
                "NumbersCount", "PatternType", "HasError", "Warnings"
            ])

            for event_id, event_data in complete_data["events"].items():
                for market_name, market_data in event_data.get("markets", {}).items():
                    has_error = "error" in market_data
                    warnings = ""
                    if market_data.get("warnings"):
                        warnings = "; ".join(market_data["warnings"])

                    writer.writerow([
                        event_id,
                        event_data.get("home_team", ""),
                        event_data.get("away_team", ""),
                        event_data.get("match_date", ""),
                        event_data.get("match_time", ""),
                        market_name,
                        market_data.get("numbers_count", 0),
                        market_data.get("market_analysis", {}).get("pattern_type", ""),
                        "YES" if has_error else "NO",
                        warnings
                    ])

        logger.info(f"CSV saved: {csv_file.name}")

        # Persist registry after saves
        self._save_scraped_registry()
        return json_file


async def main():
    parser = argparse. ArgumentParser(description="Fixed Market Data Collector with Pre-scan Optimization")
    parser.add_argument("--visible", action="store_true", help="Run with visible browser")
    parser.add_argument("--output", default="macauslot", help="Base output directory (odds go under output/odds/)")
    parser.add_argument("--registry-path", help="Custom path for scraped-events registry JSON")
    parser.add_argument("--rescrape-all", action="store_true", help="Ignore registry and scrape everything")
    parser.add_argument("--id", help="After full fetch, also save single event to market_data_single_<id>.json (under output)")
    args = parser.parse_args()

    print("=" * 70)
    print("FIXED MARKET DATA COLLECTOR - With Pre-scan Optimization")
    print("=" * 70)
    print("Phase 1: Discover all events (fast scan)")
    print("Phase 2: Filter - check registry & started matches")
    print("Phase 3: Targeted scrape - only collect new events")
    print()

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
