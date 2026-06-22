import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional


DEFAULT_CONFIG = {
    "amount_tolerance": 0.01,
    "date_tolerance_days": 3,
    "customer_name_aliases": {},
    "match_strategy": "amount_date_name",
    "session_dir": ".irec_sessions",
    "default_session": "default",
}


@dataclass
class Config:
    amount_tolerance: float = 0.01
    date_tolerance_days: int = 3
    customer_name_aliases: Dict[str, List[str]] = None
    match_strategy: str = "amount_date_name"
    session_dir: str = ".irec_sessions"
    default_session: str = "default"
    _config_path: Optional[str] = None

    def __post_init__(self):
        if self.customer_name_aliases is None:
            self.customer_name_aliases = {}

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        if config_path is None:
            config_path = cls._find_config_path()
        cfg_data = dict(DEFAULT_CONFIG)
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                cfg_data.update(user_cfg)
            except (json.JSONDecodeError, IOError):
                pass
        cfg_data["customer_name_aliases"] = cfg_data.get("customer_name_aliases", {})
        cfg = cls(**{k: v for k, v in cfg_data.items() if k in cls.__dataclass_fields__})
        cfg._config_path = config_path
        return cfg

    def save(self, config_path: Optional[str] = None):
        path = config_path or self._config_path or self._find_config_path()
        Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("_config_path", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _find_config_path() -> str:
        cwd = os.getcwd()
        return os.path.join(cwd, ".irec_config.json")

    def resolve_customer_name(self, name: str) -> str:
        if not name:
            return name
        name_stripped = name.strip().lower()
        for canonical, aliases in self.customer_name_aliases.items():
            if name_stripped == canonical.strip().lower():
                return canonical
            for alias in aliases:
                if name_stripped == alias.strip().lower():
                    return canonical
        return name.strip()

    def names_match(self, name1: str, name2: str) -> bool:
        if not name1 or not name2:
            return False
        r1 = self.resolve_customer_name(name1)
        r2 = self.resolve_customer_name(name2)
        return r1.lower() == r2.lower()

    def get_config_path(self) -> Optional[str]:
        return self._config_path
