import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
TOKEN_PATH = ROOT / "tokens.json"


class Config:
    def __init__(self, raw: dict):
        self.client_id: str = raw["client_id"]
        self.vins: list[str] = raw["vins"]
        # VIN -> what to call it on the map. BMW streams no model name, so
        # without this the only thing to label a car with is its VIN tail.
        self.names: dict[str, str] = dict(raw.get("names", {}))
        self.expected_gcid: str | None = raw.get("gcid") or None
        # expanduser before is_absolute: "~/bmwdata" is not absolute, so without
        # it the path resolves to <repo>/~/bmwdata -- real GPS traces written
        # into the checkout, in a directory .gitignore's `data/` does not cover.
        data_dir = Path(raw.get("data_dir", "data")).expanduser()
        self.data_dir = data_dir if data_dir.is_absolute() else ROOT / data_dir
        self.dsn: str = raw.get("dsn", "postgresql:///bmwcardata")
        self.retention_days: int = int(raw.get("retention_days", 30))
        # Local port for the live map, served by the menu bar app on 127.0.0.1.
        # 0 picks a free one, at the cost of the URL moving between restarts.
        self.map_port: int = int(raw.get("map_port", 8770))
        # What an index lookup costs relative to a sequential read. Postgres
        # ships 4.0, which describes a disk that has to seek; on an SSD it is
        # close to 1. Left at the default the planner will not use the index for
        # the map's biggest query. Raise it if you are genuinely on spinning rust.
        self.random_page_cost: float = float(raw.get("random_page_cost", 1.1))
        # How much history the live map carries by default. The page reloads the
        # whole payload on every update, so with a month of retention it would
        # be shipping and re-parsing megabytes every time the car reports.
        # A week covers "what have I been doing lately"; ?days=0 fetches the lot.
        self.map_days: int = int(raw.get("map_days", 7))
        # Logs are rotated on size, not age: the stream prints a line per
        # message, so volume tracks how much you drive rather than the calendar.
        self.log_max_mb: float = float(raw.get("log_max_mb", 10))
        self.log_keep: int = int(raw.get("log_keep", 3))
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
    try:
        with CONFIG_PATH.open("rb") as fh:
            return Config(tomllib.load(fh))
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{CONFIG_PATH} is not valid TOML: {exc}") from exc
    except KeyError as exc:
        raise SystemExit(f"{CONFIG_PATH} is missing required setting {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"{CONFIG_PATH} has an unusable value: {exc}") from exc


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
    # Escaped, like set_name does. A dsn with a password containing a quote or a
    # backslash -- postgresql://u:pa"ss@host -- otherwise wrote a line that made
    # the entire config unparseable, and the next load failed on a file the user
    # never edited by hand.
    rendered = f'"{_toml_escape(value)}"' if isinstance(value, str) else str(value)
    lines = CONFIG_PATH.read_text().splitlines()

    def key_of(line: str) -> str:
        stripped = line.lstrip()
        # Also match a commented-out default, so setting it uncomments the line.
        bare = stripped[1:].lstrip() if stripped.startswith("#") else stripped
        return bare.split("=")[0].strip()

    # A live assignment wins over a commented default. Taking whichever came
    # first meant that with a `# map_port = 8770` hint above a real `map_port =
    # 9000`, setting it uncommented the hint and left the real line in place --
    # two assignments of the same key, which TOML rejects outright.
    target = next(
        (i for i, line in enumerate(lines)
         if key_of(line) == key and not line.lstrip().startswith("#")),
        None,
    )
    if target is None:
        target = next((i for i, line in enumerate(lines) if key_of(line) == key), None)

    if target is not None:
        lines[target] = f"{key} = {rendered}"
        CONFIG_PATH.write_text("\n".join(lines) + "\n")
        return

    # Absent entirely: insert before the first table header, since a top-level
    # scalar below one would be read as a member of that table.
    out, placed = [], False
    for line in lines:
        if not placed and line.lstrip().startswith("["):
            out.append(f"{key} = {rendered}")
            placed = True
        out.append(line)
    if not placed:
        out.append(f"{key} = {rendered}")
    CONFIG_PATH.write_text("\n".join(out) + "\n")


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def set_name(vin: str, name: str) -> None:
    """Name a vehicle in the [names] table, or clear the name if empty.

    Same text-editing approach as set_value, for the same reason. [names] lives
    at the end of the file because everything after a TOML table header belongs
    to that table -- appending a name anywhere else would silently swallow the
    settings below it.
    """
    ensure()
    lines = CONFIG_PATH.read_text().splitlines()
    key = f'"{_toml_escape(vin)}"'
    rendered = f'{key} = "{_toml_escape(name)}"'

    out, in_names, written = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            # Leaving the table without having found the VIN: add it here, while
            # we are still inside it.
            if in_names and not written and name:
                out.append(rendered)
                written = True
            in_names = stripped == "[names]"
            out.append(line)
            continue
        # Match the VIN whether it was written quoted or bare, and whether or
        # not the line is commented out.
        bare = stripped[1:].lstrip() if stripped.startswith("#") else stripped
        candidate = bare.split("=")[0].strip()
        if in_names and candidate in (key, vin):
            if name and not written:
                out.append(rendered)
                written = True
            continue  # renaming to "" simply drops the line
        out.append(line)

    if in_names and not written and name:
        out.append(rendered)
        written = True
    if not written and name:
        out.append("")
        out.append("[names]")
        out.append(rendered)
    CONFIG_PATH.write_text("\n".join(out) + "\n")
