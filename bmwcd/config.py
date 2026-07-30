import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
TOKEN_PATH = ROOT / "tokens.json"


class Config:
    def __init__(self, raw: dict):
        self.client_id: str = raw["client_id"]
        self.vins: list[str] = raw["vins"]
        self.expected_gcid: str | None = raw.get("gcid") or None
        data_dir = Path(raw.get("data_dir", "data"))
        self.data_dir = data_dir if data_dir.is_absolute() else ROOT / data_dir
        self.dsn: str = raw.get("dsn", "postgresql:///bmwcardata")
        # Rebuild the connection if the broker holds the socket open but stops
        # publishing for this long. Generous by default: a parked car is
        # genuinely silent for hours, and a short timer would churn all night.
        self.stall_timeout: float = float(raw.get("stall_timeout_hours", 6)) * 3600
        self.retention_days: int = int(raw.get("retention_days", 30))
        # Raw JSONL is the rebuild path if the schema turns out wrong, so it
        # outlives the database by default.
        self.raw_retention_days: int = int(
            raw.get("raw_retention_days", self.retention_days * 2)
        )


def load() -> Config:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"No {CONFIG_PATH}. Copy config.example.toml to config.toml and fill it in."
        )
    with CONFIG_PATH.open("rb") as fh:
        return Config(tomllib.load(fh))
