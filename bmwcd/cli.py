import sys
from datetime import date, datetime, timedelta

from . import auth, config, db, export, stream

USAGE = """usage: python -m bmwcd <command>

  auth     run the OAuth2 device-code flow (one-off, and after ~2 weeks idle)
  stream   subscribe to the CarData stream -> raw JSONL + Postgres
  status   show token state and row counts without connecting
  initdb   create the schema (idempotent)
  load     backfill Postgres from the raw JSONL sink (idempotent)
  prune    drop rows and raw files past their retention windows
  export   render the map page to data/viz/map.html  [--days N]
"""


def _raw_files(cfg):
    return sorted((cfg.data_dir / "raw").glob("*.jsonl"))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    cmd = argv[0]
    cfg = config.load()
    store = auth.TokenStore(config.TOKEN_PATH, cfg.client_id)

    if cmd == "auth":
        tokens = auth.device_flow(store)
        print(f"\nAuthorised. GCID: {tokens.gcid}")
        print(f"Tokens written to {config.TOKEN_PATH}")
        return 0

    if cmd == "status":
        tokens = store.load()
        if tokens is None:
            print("No tokens. Run: python -m bmwcd auth")
            return 1
        left = int(tokens.seconds_left())
        print(f"GCID:      {tokens.gcid}")
        print(f"id_token:  {'valid' if left > 0 else 'EXPIRED'} ({left}s)")
        print(f"VINs:      {', '.join(cfg.vins)}")
        try:
            with db.connect(cfg) as conn:
                rows = conn.execute("SELECT count(*) FROM telemetry").fetchone()[0]
                span = conn.execute(
                    "SELECT min(ts), max(ts) FROM telemetry"
                ).fetchone()
                keys = conn.execute(
                    "SELECT count(DISTINCT key) FROM telemetry"
                ).fetchone()[0]
            print(f"rows:      {rows} ({keys} distinct keys)")
            if span[0]:
                print(f"span:      {span[0]:%Y-%m-%d %H:%M} .. {span[1]:%Y-%m-%d %H:%M}")
        except Exception as exc:  # noqa: BLE001
            print(f"database:  unavailable ({exc})")
        return 0

    if cmd == "stream":
        stream.run(cfg, store)
        return 0

    if cmd == "initdb":
        db.init(cfg)
        print(f"Schema applied to {cfg.dsn}")
        return 0

    if cmd == "load":
        files = _raw_files(cfg)
        if not files:
            print("No raw JSONL to load.")
            return 0
        seen, written = db.load_jsonl(cfg, files)
        print(f"Read {seen} messages from {len(files)} file(s); {written} rows inserted.")
        return 0

    if cmd == "prune":
        deleted = db.prune(cfg)
        print(f"Deleted {deleted} rows older than {cfg.retention_days} days.")
        cutoff = date.today() - timedelta(days=cfg.raw_retention_days)
        removed = 0
        for path in _raw_files(cfg):
            try:
                stamp = datetime.strptime(path.stem, "cardata-%Y-%m-%d").date()
            except ValueError:
                continue
            if stamp < cutoff:
                path.unlink()
                removed += 1
        print(f"Removed {removed} raw file(s) older than {cfg.raw_retention_days} days.")
        return 0

    if cmd == "export":
        days = None
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        out, data = export.render(cfg, days)
        fixes = sum(len(v["points"]) for v in data["vehicles"])
        print(f"Wrote {out} ({len(data['vehicles'])} vehicle(s), {fixes} fixes)")
        for note in data["notes"]:
            print(f"  note: {note}")
        return 0

    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
