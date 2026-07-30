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
        # expanduser before is_absolute: "~/bmwdata" is not absolute, so without
        # it the path resolves to <repo>/~/bmwdata -- real GPS traces written
        # into the checkout, in a directory .gitignore's `data/` does not cover.
        data_dir = Path(raw.get("data_dir", "data")).expanduser()
        self.data_dir = data_dir if data_dir.is_absolute() else ROOT / data_dir
        self.dsn: str = raw.get("dsn", "postgresql:///bmwcardata")
        self.retention_days: int = int(raw.get("retention_days", 30))
        # Raw JSONL is the rebuild path if the schema turns out wrong, so it
        # outlives the database by default.
        self.raw_retention_days: int = int(
            raw.get("raw_retention_days", self.retention_days * 2)
        )


EXAMPLE_PATH = ROOT / "config.example.toml"


def exists() -> bool:
    return CONFIG_PATH.exists()


def load() -> Config:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"No {CONFIG_PATH}. Copy config.example.toml to config.toml and fill it in."
        )
    with CONFIG_PATH.open("rb") as fh:
        return Config(tomllib.load(fh))


def ensure() -> Path:
    """Create config.toml from the example if it is missing."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(EXAMPLE_PATH.read_text())
    return CONFIG_PATH


def set_value(key: str, value) -> None:
    """Set a top-level scalar in config.toml, in place.

    Edited as text rather than round-tripped through a TOML writer: the file is
    mostly comments explaining each setting, and every serialiser would discard
    them. Only simple top-level scalars are supported, which is all the GUI sets.
    """
    ensure()
    rendered = f'"{value}"' if isinstance(value, str) else str(value)
    lines = CONFIG_PATH.read_text().splitlines()
    out, replaced = [], False
    for line in lines:
        stripped = line.lstrip()
        # Also match a commented-out default so setting it uncomments the line.
        bare = stripped[1:].lstrip() if stripped.startswith("#") else stripped
        if not replaced and bare.split("=")[0].strip() == key:
            out.append(f"{key} = {rendered}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key} = {rendered}")
    CONFIG_PATH.write_text("\n".join(out) + "\n")
