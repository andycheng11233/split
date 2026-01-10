#!/usr/bin/env python3
"""
Alias Helper - Unified module for managing alias.json across all scrapers.

Features:
- Load and match teams/leagues from alias.json
- Add new teams/leagues with proper format (country, league, sources)
- Track unmatched entries for review
- Thread-safe file operations
"""

import json
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime

ALIAS_FILE = "alias.json"
UNMATCHED_FILE = "unmatched_entries.json"

_file_lock = threading.Lock()


def _norm_key(val: str) -> str:
    """Normalize string for matching: lowercase, no spaces."""
    return re.sub(r"\s+", "", val.lower()) if val else ""


class AliasHelper:
    """Helper class for managing alias.json entries."""
    
    def __init__(self, alias_file: str = ALIAS_FILE, auto_save: bool = False):
        self.alias_file = Path(alias_file)
        self.auto_save = auto_save
        self.data: Dict = {"teams": {}, "leagues": {}}
        self.alias_map: Dict[str, str] = {}
        self.league_map: Dict[str, str] = {}
        self.unmatched_teams: Set[str] = set()
        self.unmatched_leagues: Set[str] = set()
        self.new_entries_count = 0
        self._load()
    
    def _load(self):
        """Load alias.json and build lookup maps."""
        if self.alias_file.exists():
            try:
                with open(self.alias_file, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
            except Exception as e:
                print(f"Warning: Failed to load {self.alias_file}: {e}")
                self.data = {"teams": {}, "leagues": {}}
        self._build_maps()
    
    def _build_maps(self):
        """Build lookup maps from loaded data."""
        self.alias_map = {}
        self.league_map = {}
        
        for canon, entry in self.data.get("teams", {}).items():
            key = _norm_key(canon)
            if key:
                self.alias_map[key] = canon
            for v in (entry or {}).get("variants", []) or []:
                k = _norm_key(v)
                if k:
                    self.alias_map[k] = canon
            for src, lst in ((entry or {}).get("sources") or {}).items():
                for v in lst or []:
                    k = _norm_key(v)
                    if k:
                        self.alias_map[k] = canon
        
        for canon, entry in self.data.get("leagues", {}).items():
            key = _norm_key(canon)
            if key:
                self.league_map[key] = canon
            for v in (entry or {}).get("variants", []) or []:
                k = _norm_key(v)
                if k:
                    self.league_map[k] = canon
            for src, lst in ((entry or {}).get("sources") or {}).items():
                for v in lst or []:
                    k = _norm_key(v)
                    if k:
                        self.league_map[k] = canon
    
    def match_team(self, name: str) -> str:
        """Match a team name to its canonical form."""
        if not name:
            return ""
        k = _norm_key(name)
        return self.alias_map.get(k, "")
    
    def match_league(self, name: str) -> str:
        """Match a league name to its canonical form."""
        if not name:
            return ""
        k = _norm_key(name)
        return self.league_map.get(k, "")
    
    def team_exists(self, name: str) -> bool:
        """Check if a team exists in alias.json."""
        return bool(self.match_team(name))
    
    def league_exists(self, name: str) -> bool:
        """Check if a league exists in alias.json."""
        return bool(self.match_league(name))
    
    def add_team(
        self,
        name: str,
        source: str,
        country: str = "",
        league: str = "",
        is_womens: bool = False,
        is_national: bool = False,
    ) -> bool:
        """Add a new team to alias.json or update existing entry with new source."""
        if not name or not source:
            return False
        
        name = name.strip()
        if not name:
            return False
        
        canonical = self.match_team(name)
        
        if canonical:
            entry = self.data["teams"][canonical]
            sources = entry.setdefault("sources", {})
            src_list = sources.setdefault(source, [])
            if name not in src_list:
                src_list.append(name)
                if self.auto_save:
                    self.save()
                return True
            return False
        
        entry = {
            "variants": [name],
            "sources": {source: [name]},
        }
        
        if country:
            entry["country"] = country
        if league:
            entry["primary_league"] = league
        if is_womens:
            entry["is_womens_team"] = True
        if is_national:
            entry["is_national_team"] = True
        
        if not is_womens and self._is_womens_team_name(name):
            entry["is_womens_team"] = True
        
        if not country and league:
            inferred_country = self._infer_country_from_league(league)
            if inferred_country:
                entry["country"] = inferred_country
        
        self.data["teams"][name] = entry
        k = _norm_key(name)
        self.alias_map[k] = name
        self.new_entries_count += 1
        
        if self.auto_save:
            self.save()
        
        return True
    
    def add_league(
        self,
        name: str,
        source: str,
        country: str = "",
    ) -> bool:
        """Add a new league to alias.json or update existing entry with new source."""
        if not name or not source:
            return False
        
        name = name.strip()
        if not name:
            return False
        
        canonical = self.match_league(name)
        
        if canonical:
            entry = self.data["leagues"][canonical]
            sources = entry.setdefault("sources", {})
            src_list = sources.setdefault(source, [])
            if name not in src_list:
                src_list.append(name)
                if self.auto_save:
                    self.save()
                return True
            return False
        
        entry = {
            "variants": [name],
            "sources": {source: [name]},
        }
        
        if country:
            entry["country"] = country
        else:
            inferred = self._infer_country_from_league(name)
            if inferred:
                entry["country"] = inferred
        
        self.data["leagues"][name] = entry
        k = _norm_key(name)
        self.league_map[k] = name
        self.new_entries_count += 1
        
        if self.auto_save:
            self.save()
        
        return True
    
    def add_team_if_new(
        self,
        name: str,
        source: str,
        country: str = "",
        league: str = "",
    ) -> Tuple[str, bool]:
        """Add team if new, return (canonical_name, was_new)."""
        if not name:
            return "", False
        
        canonical = self.match_team(name)
        if canonical:
            self.add_team(name, source)
            return canonical, False
        
        self.add_team(name, source, country=country, league=league)
        return name, True
    
    def add_league_if_new(
        self,
        name: str,
        source: str,
        country: str = "",
    ) -> Tuple[str, bool]:
        """Add league if new, return (canonical_name, was_new)."""
        if not name:
            return "", False
        
        canonical = self.match_league(name)
        if canonical:
            self.add_league(name, source)
            return canonical, False
        
        self.add_league(name, source, country=country)
        return name, True
    
    def track_unmatched(self, name: str, is_league: bool = False):
        """Track an unmatched team/league for later review."""
        if not name:
            return
        if is_league:
            self.unmatched_leagues.add(name)
        else:
            self.unmatched_teams.add(name)
    
    def save(self):
        """Save alias.json with thread safety."""
        with _file_lock:
            if self.alias_file.exists():
                backup_path = self.alias_file.with_suffix('.backup.json')
                try:
                    backup_path.write_text(
                        self.alias_file.read_text(encoding='utf-8'),
                        encoding='utf-8'
                    )
                except Exception:
                    pass
            
            with open(self.alias_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
    
    def save_unmatched(self, filepath: str = UNMATCHED_FILE):
        """Save unmatched entries to a separate file for review."""
        if not self.unmatched_teams and not self.unmatched_leagues:
            return
        
        data = {
            "timestamp": datetime.now().isoformat(),
            "unmatched_teams": sorted(self.unmatched_teams),
            "unmatched_leagues": sorted(self.unmatched_leagues),
        }
        
        Path(filepath).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
    
    def get_stats(self) -> Dict:
        """Get statistics about the alias data."""
        teams = self.data.get("teams", {})
        leagues = self.data.get("leagues", {})
        
        teams_no_country = sum(1 for e in teams.values() if not e.get("country"))
        teams_no_league = sum(1 for e in teams.values() 
                             if not e.get("primary_league") and not e.get("is_national_team"))
        leagues_no_country = sum(1 for e in leagues.values() if not e.get("country"))
        
        return {
            "total_teams": len(teams),
            "total_leagues": len(leagues),
            "teams_missing_country": teams_no_country,
            "teams_missing_league": teams_no_league,
            "leagues_missing_country": leagues_no_country,
            "new_entries_this_session": self.new_entries_count,
            "unmatched_teams": len(self.unmatched_teams),
            "unmatched_leagues": len(self.unmatched_leagues),
        }
    
    def _is_womens_team_name(self, name: str) -> bool:
        """Check if team name indicates a women's team."""
        patterns = [
            r'女[足子隊]',
            r'女子',
            r'\(女\)',
            r'（女）',
            r'Women',
            r'Ladies',
            r'Femenino',
            r'Femminile',
            r'Frauen',
        ]
        for p in patterns:
            if re.search(p, name, re.IGNORECASE):
                return True
        return False
    
    def _infer_country_from_league(self, league: str) -> str:
        """Try to infer country from league name."""
        if not league:
            return ""
        
        country_patterns = {
            '英格蘭': [r'^英格蘭', r'^英超', r'^英甲', r'^英乙', r'^英冠'],
            '西班牙': [r'^西班牙', r'^西甲', r'^西乙'],
            '德國': [r'^德國', r'^德甲', r'^德乙'],
            '意大利': [r'^意大利', r'^意甲', r'^意乙'],
            '法國': [r'^法國', r'^法甲', r'^法乙'],
            '葡萄牙': [r'^葡萄牙', r'^葡超'],
            '荷蘭': [r'^荷蘭', r'^荷甲', r'^荷乙'],
            '比利時': [r'^比利時'],
            '蘇格蘭': [r'^蘇格蘭', r'^蘇超'],
            '澳洲': [r'^澳洲', r'^澳超', r'^澳職'],
            '日本': [r'^日本', r'^日職', r'^日皇', r'^日聯'],
            '南韓': [r'^南韓', r'^韓國', r'^韓職', r'^韓K'],
            '美國': [r'^美國', r'^美職', r'^美聯'],
            '墨西哥': [r'^墨西哥'],
            '巴西': [r'^巴西'],
            '阿根廷': [r'^阿根廷'],
            '中國': [r'^中國', r'^中超', r'^中甲'],
            '沙特阿拉伯': [r'^沙特', r'^沙地'],
            '阿聯酋': [r'^阿聯酋'],
            '卡塔爾': [r'^卡塔爾'],
            '泰國': [r'^泰國', r'^泰超', r'^泰甲'],
            '印尼': [r'^印尼'],
            '馬來西亞': [r'^馬來', r'^馬超'],
            '俄羅斯': [r'^俄羅斯', r'^俄超'],
            '土耳其': [r'^土耳其', r'^土超'],
            '瑞典': [r'^瑞典'],
            '挪威': [r'^挪威'],
            '芬蘭': [r'^芬蘭'],
            '智利': [r'^智利'],
            '歐洲': [r'^歐洲', r'^歐冠', r'^歐霸', r'^歐協'],
            '亞洲': [r'^亞洲', r'^亞冠', r'^亞協'],
            '非洲': [r'^非洲'],
            '南美洲': [r'^南美'],
            '國際': [r'^國際', r'^FIFA', r'^世界'],
        }
        
        for country, patterns in country_patterns.items():
            for p in patterns:
                if re.search(p, league):
                    return country
        
        return ""


# Convenience functions for backward compatibility
_default_helper: Optional[AliasHelper] = None


def get_helper(alias_file: str = ALIAS_FILE) -> AliasHelper:
    """Get or create the default AliasHelper instance."""
    global _default_helper
    if _default_helper is None:
        _default_helper = AliasHelper(alias_file)
    return _default_helper


def load_alias_map(alias_file: str = ALIAS_FILE) -> Dict[str, str]:
    """Load alias map (backward compatible function)."""
    return get_helper(alias_file).alias_map


def alias_match(name: str, alias_map: Dict[str, str] = None) -> str:
    """Match a name to its canonical form (backward compatible function)."""
    if alias_map is not None:
        k = _norm_key(name)
        return alias_map.get(k, "")
    return get_helper().match_team(name)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Alias Helper CLI")
    parser.add_argument("--stats", action="store_true", help="Show alias.json statistics")
    parser.add_argument("--match", help="Match a team name")
    parser.add_argument("--match-league", help="Match a league name")
    args = parser.parse_args()
    
    helper = AliasHelper()
    
    if args.stats:
        stats = helper.get_stats()
        print("=" * 50)
        print("ALIAS.JSON STATISTICS")
        print("=" * 50)
        for k, v in stats.items():
            print(f"  {k}: {v}")
    
    if args.match:
        result = helper.match_team(args.match)
        if result:
            print(f"'{args.match}' -> '{result}'")
        else:
            print(f"'{args.match}' not found")
    
    if args.match_league:
        result = helper.match_league(args.match_league)
        if result:
            print(f"'{args.match_league}' -> '{result}'")
        else:
            print(f"'{args.match_league}' not found")
