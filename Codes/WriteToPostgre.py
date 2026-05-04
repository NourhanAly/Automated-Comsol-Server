from __future__ import annotations

import configparser
import csv
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import psycopg


# ----------------------------
# Helpers
# ----------------------------

_SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$", re.IGNORECASE)


def _sanitize_ident(name: str) -> str:
    """
    Keep identifiers safe and predictable:
    - Lowercase
    - Replace invalid chars with underscore
    - Ensure it starts with a letter/underscore
    """
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s)
    if not re.match(r"^[a-z_][a-z0-9_]*$", s):
        raise ValueError(f"Unsafe identifier after sanitization: {name!r} -> {s!r}")
    return s


def _quoted_ident(ident: str) -> str:
    # We sanitize, so simple quoting is OK
    return f'"{ident}"'


def _read_multiline_json_dict(path: Union[str, Path]) -> Dict[str, Any]:
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError(f"{path}: expected top-level JSON object/dict, got {type(obj).__name__}")
    return obj


def _detect_csv_header_and_ncols(csv_path: Union[str, Path]) -> Tuple[bool, int]:
    """
    Uses csv.Sniffer to guess if there's a header, and counts columns from first row.
    This is heuristic; you can override behavior by ensuring your files are consistent.
    """
    csv_path = Path(csv_path)
    sample = csv_path.read_bytes()[:65536].decode("utf-8", errors="replace")

    sniffer = csv.Sniffer()
    try:
        has_header = sniffer.has_header(sample)
    except csv.Error:
        has_header = False

    # Count columns from the first non-empty row
    reader = csv.reader(io.StringIO(sample))
    for row in reader:
        if row and any(cell.strip() for cell in row):
            return has_header, len(row)

    raise ValueError(f"{csv_path}: could not read any rows to determine column count")


def _make_column_names(ncols: int, has_header: bool, csv_path: Union[str, Path]) -> Tuple[bool, Tuple[str, ...]]:
    """
    If header exists, read the first row as names; otherwise generate col1..colN.
    Returns (has_header_used, colnames)
    """
    csv_path = Path(csv_path)
    with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        first = next(reader, None)

    if first is None:
        raise ValueError(f"{csv_path}: empty file")

    if has_header:
        # sanitize header names; if duplicates/empties, fall back to col1..colN
        cleaned = []
        seen = set()
        for i, h in enumerate(first, start=1):
            h = (h or "").strip()
            if not h:
                h = f"col{i}"
            h = _sanitize_ident(h)
            if h in seen:
                h = f"{h}_{i}"
            seen.add(h)
            cleaned.append(h)
        if len(cleaned) != ncols:
            has_header = False
        else:
            return True, tuple(cleaned)

    return False, tuple(f"col{i}" for i in range(1, ncols + 1))


def _copy_csv_into_table(
    conn: psycopg.Connection,
    *,
    schema: str,
    table: str,
    csv_path: Union[str, Path],
    expected_cols: int,
    if_exists: str,
) -> Dict[str, Any]:
    """
    Creates table (TEXT columns) and COPYs CSV into it.
    """
    if if_exists not in {"replace", "append", "fail"}:
        raise ValueError("if_exists must be one of: replace | append | fail")

    schema = _sanitize_ident(schema)
    table = _sanitize_ident(table)
    csv_path = Path(csv_path)

    guessed_has_header, ncols = _detect_csv_header_and_ncols(csv_path)
    if ncols != expected_cols:
        raise ValueError(f"{csv_path}: expected {expected_cols} columns, detected {ncols}")

    has_header_used, colnames = _make_column_names(ncols, guessed_has_header, csv_path)

    full_table = f'{_quoted_ident(schema)}.{_quoted_ident(table)}'

    cols_sql = ", ".join(f'{_quoted_ident(c)} TEXT' for c in colnames)

    with conn.cursor() as cur:
        if if_exists == "replace":
            cur.execute(f"DROP TABLE IF EXISTS {full_table};")

        if if_exists == "fail":
            # Create will fail if table exists
            cur.execute(f"CREATE TABLE {full_table} ({cols_sql});")
        else:
            # append or replace
            cur.execute(f"CREATE TABLE IF NOT EXISTS {full_table} ({cols_sql});")

        # COPY FROM file
        copy_sql = f"COPY {full_table} ({', '.join(_quoted_ident(c) for c in colnames)}) FROM STDIN WITH (FORMAT csv"
        if has_header_used:
            copy_sql += ", HEADER true"
        copy_sql += ")"

        with cur.copy(copy_sql) as cp:
            with csv_path.open("rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    cp.write(chunk)

    return {
        "table": f"{schema}.{table}",
        "columns": list(colnames),
        "has_header": bool(has_header_used),
    }


def _upsert_metadata(
    conn: psycopg.Connection,
    *,
    schema: str,
    table: str,
    meta: Dict[str, Any],
    if_exists: str,
) -> None:
    """
    (Kept for reference) Stores metadata as key/value rows.
    Not used anymore by upload_cycle_soc_tables_psycopg.
    """
    if if_exists not in {"replace", "append", "fail"}:
        raise ValueError("if_exists must be one of: replace | append | fail")

    schema = _sanitize_ident(schema)
    table = _sanitize_ident(table)
    full_table = f'{_quoted_ident(schema)}.{_quoted_ident(table)}'

    with conn.cursor() as cur:
        if if_exists == "replace":
            cur.execute(f"DROP TABLE IF EXISTS {full_table};")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {full_table} (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        if if_exists == "fail":
            # Force failure if exists (this is a bit odd; left as-is)
            cur.execute(f"ALTER TABLE {full_table} ADD CONSTRAINT __exists_check UNIQUE (key);")

        if not meta:
            return

        rows = [(str(k), str(v)) for k, v in meta.items()]

        cur.executemany(
            f"""
            INSERT INTO {full_table} (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            rows,
        )


# -------------------------------------------------------------------
# (1) NEW: store raw metadata text exactly as the original file
# -------------------------------------------------------------------
def _store_metadata_raw(
    conn: psycopg.Connection,
    *,
    schema: str,
    table: str,
    metadata_path: Union[str, Path],
    if_exists: str,
) -> None:
    """
    Stores the metadata file EXACTLY as-is (byte-for-byte as text) into Postgres.
    One row per upload, in column raw_text.

    This preserves:
      - braces
      - quotes
      - commas
      - indentation
      - ordering
      - line breaks
    """
    if if_exists not in {"replace", "append", "fail"}:
        raise ValueError("if_exists must be one of: replace | append | fail")

    schema = _sanitize_ident(schema)
    table = _sanitize_ident(table)
    full_table = f'{_quoted_ident(schema)}.{_quoted_ident(table)}'

    raw = Path(metadata_path).read_text(encoding="utf-8")  # EXACT FILE CONTENT

    with conn.cursor() as cur:
        if if_exists == "replace":
            cur.execute(f"DROP TABLE IF EXISTS {full_table};")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {full_table} (
                id BIGSERIAL PRIMARY KEY,
                raw_text TEXT NOT NULL
            );
            """
        )

        if if_exists == "fail":
            # fail if table already has any rows
            cur.execute(f"SELECT 1 FROM {full_table} LIMIT 1;")
            if cur.fetchone():
                raise RuntimeError(f"{full_table} already contains data (if_exists='fail').")

        # insert a new row (append semantics)
        cur.execute(f"INSERT INTO {full_table} (raw_text) VALUES (%s);", (raw,))


# ----------------------------
# Config
# ----------------------------

# UPDATED: allow missing uploads to return None for those fields
@dataclass(frozen=True)
class UploadResult:
    schema: str
    n: int
    m: int
    eis_table: Optional[str]
    cycle_table: Optional[str]
    metadata_table: Optional[str]
    eis_info: Optional[Dict[str, Any]]
    cycle_info: Optional[Dict[str, Any]]
    metadata_rows: int


def read_ini_for_upload(config_path: Union[str, Path]) -> Tuple[Dict[str, Any], str]:
    """
    Reads:
      [postgres] : host, port, dbname, user, password, sslmode, sslrootcert(optional)
      [watch]    : schema   (preferred, matches your working watcher)
    Fallback: [postgres].schema if present.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")

    if "postgres" not in cfg:
        raise ValueError("Config must contain [postgres] section.")

    pg = cfg["postgres"]

    connect_kwargs: Dict[str, Any] = dict(
        host=pg.get("host"),
        port=pg.getint("port", fallback=5432),
        dbname=pg.get("dbname"),
        user=pg.get("user"),
        password=pg.get("password"),
        sslmode=pg.get("sslmode", fallback="require"),
    )

    sslrootcert = pg.get("sslrootcert", fallback=None)
    if sslrootcert:
        connect_kwargs["sslrootcert"] = sslrootcert

    for k in ("host", "dbname", "user", "password"):
        if not connect_kwargs.get(k):
            raise ValueError(f"Missing postgres.{k} in config.")

    schema = None
    if "watch" in cfg:
        schema = cfg["watch"].get("schema", fallback=None)
    if not schema:
        schema = pg.get("schema", fallback=None)

    if not schema:
        raise ValueError("Schema not found. Add [watch].schema (recommended) or [postgres].schema")

    return connect_kwargs, schema


# ----------------------------
# Public API
# ----------------------------

# UPDATED: make file paths optional (Option A)
def upload_cycle_soc_tables_psycopg(
    *,
    config_path: Union[str, Path],
    cell: int,
    n: int,
    m: int,
    eis_csv_path: Optional[Union[str, Path]] = None,
    cycle_csv_path: Optional[Union[str, Path]] = None,
    metadata_json_path: Optional[Union[str, Path]] = None,
    if_exists: str = "replace",
) -> UploadResult:

    if not isinstance(n, int) or not isinstance(m, int):
        raise TypeError("n and m must be integers")

    connect_kwargs, schema = read_ini_for_upload(config_path)
    schema = _sanitize_ident(schema)

    eis_table = _sanitize_ident(f"eis{cell}cycle{n}soc{m}")
    cycle_table = _sanitize_ident(f"cycling{cell}cycle{n}soc{m}")
    meta_table = _sanitize_ident(f"metadata{cell}cycle{n}soc{m}")

    eis_info: Optional[Dict[str, Any]] = None
    cycle_info: Optional[Dict[str, Any]] = None
    metadata_rows = 0

    with psycopg.connect(**connect_kwargs) as conn:
        # Make everything atomic
        with conn.transaction():

            # Only upload if path provided
            if eis_csv_path is not None:
                eis_info = _copy_csv_into_table(
                    conn,
                    schema=schema,
                    table=eis_table,
                    csv_path=eis_csv_path,
                    expected_cols=3,
                    if_exists=if_exists,
                )

            # Only upload if path provided
            if cycle_csv_path is not None:
                cycle_info = _copy_csv_into_table(
                    conn,
                    schema=schema,
                    table=cycle_table,
                    csv_path=cycle_csv_path,
                    expected_cols=2,
                    if_exists=if_exists,
                )

            # Only upload if path provided
            if metadata_json_path is not None:
                _store_metadata_raw(
                    conn,
                    schema=schema,
                    table=meta_table,
                    metadata_path=metadata_json_path,
                    if_exists=if_exists,
                )
                metadata_rows = 1

    return UploadResult(
        schema=schema,
        n=n,
        m=m,
        eis_table=f"{schema}.{eis_table}" if eis_csv_path is not None else None,
        cycle_table=f"{schema}.{cycle_table}" if cycle_csv_path is not None else None,
        metadata_table=f"{schema}.{meta_table}" if metadata_json_path is not None else None,
        eis_info=eis_info,
        cycle_info=cycle_info,
        metadata_rows=metadata_rows,
    )



loc = str(Path.cwd())


result_all = upload_cycle_soc_tables_psycopg(
    config_path=loc + r"\db_write.ini",
    cell=50,
    n=500,
    m=0,
    eis_csv_path=loc + r"\\ExperimentalData\\csv\\N_49_Cycle_100_SoC_75.csv",
    cycle_csv_path=loc + r"\\ExperimentalData\\csv\\DISCHARGE_CYCLE_100.csv",
    metadata_json_path=loc + r"\InputMeta.txt",
    if_exists="replace",
)
print(result_all)



