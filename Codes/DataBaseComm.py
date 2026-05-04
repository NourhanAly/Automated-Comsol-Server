from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set, Dict, Any, Tuple, List
import configparser
import json
import time
import re
import psycopg
import io
import pandas as pd
from psycopg import sql

from dataclasses import dataclass
import configparser
import threading
import time
import uuid



# ----------------------------
# Config reader (reuse yours if you already have it)
# ----------------------------

def _read_config(config_path: str | Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
    config_path = Path(config_path)
    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")

    pg = cfg["postgres"]
    watch = cfg["watch"]

    connect_kwargs: Dict[str, Any] = dict(
        host=pg.get("host"),
        port=pg.getint("port", fallback=5432),
        dbname=pg.get("dbname"),
        user=pg.get("user"),
        password=pg.get("password"),
        sslmode=pg.get("sslmode", fallback="require"),
    )

    schema = watch.get("schema")
    if not schema:
        raise ValueError("Missing [watch].schema in config")

    return connect_kwargs, {"schema": schema}


# ----------------------------
# Status log table
# ----------------------------

LOG_TABLE_NAME = "case_status_log"


def ensure_status_log_table(conn: psycopg.Connection, schema: str) -> None:
    """
    Creates a status log table once.
    One row per (run_id, cell, cycle, soc). We UPSERT so it keeps updating the same row.
    """
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}";')
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS "{schema}"."{LOG_TABLE_NAME}" (
                run_id          TEXT NOT NULL,
                cell            INT  NOT NULL,
                cycle           INT  NOT NULL,
                soc             INT  NOT NULL,

                stage           TEXT NOT NULL,
                message         TEXT NOT NULL DEFAULT '',
                last_heartbeat  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at     TIMESTAMPTZ NULL,
                ok              BOOLEAN NULL,

                PRIMARY KEY (run_id, cell, cycle, soc)
            );
            """
        )


def upsert_case_status(
    config_path: str | Path,
    *,
    run_id: str,
    cell: int,
    n: int,
    m: int,
    stage: str,
    message: str = "",
    finished: bool = False,
    ok: Optional[bool] = None,
) -> None:
    """
    Writes a heartbeat/status update.
    Safe to call repeatedly; updates last_heartbeat and stage/message.
    """
    connect_kwargs, watch = _read_config(config_path)
    schema = watch["schema"]

    with psycopg.connect(**connect_kwargs) as conn:
        with conn.transaction():
            ensure_status_log_table(conn, schema)

            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO "{schema}"."{LOG_TABLE_NAME}"
                        (run_id, cell, cycle, soc, stage, message, last_heartbeat, started_at, finished_at, ok)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, NOW(), NOW(), %s, %s)
                    ON CONFLICT (run_id, cell, cycle, soc)
                    DO UPDATE SET
                        stage = EXCLUDED.stage,
                        message = EXCLUDED.message,
                        last_heartbeat = NOW(),
                        finished_at = COALESCE(EXCLUDED.finished_at, "{schema}"."{LOG_TABLE_NAME}".finished_at),
                        ok = COALESCE(EXCLUDED.ok, "{schema}"."{LOG_TABLE_NAME}".ok);
                    """,
                    (
                        run_id,
                        int(cell),
                        int(n),
                        int(m),
                        str(stage),
                        str(message or ""),
                        (time.strftime("%Y-%m-%d %H:%M:%S%z") if finished else None),
                        ok,
                    ),
                )


# ----------------------------
# Heartbeat helper (updates every 10 minutes)
# ----------------------------

class CaseHeartbeat:
    """
    Runs a background thread that updates the DB every interval_seconds.
    You can also push stage updates immediately via .update(...)
    """
    def __init__(
        self,
        config_path: str | Path,
        *,
        cell: int,
        n: int,
        m: int,
        interval_seconds: int = 600,  # 10 minutes
    ):
        self.config_path = config_path
        self.cell = int(cell)
        self.n = int(n)
        self.m = int(m)
        self.interval_seconds = int(interval_seconds)

        self.run_id = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stage = "starting"
        self._message = ""

        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        upsert_case_status(
            self.config_path,
            run_id=self.run_id,
            cell=self.cell,
            n=self.n,
            m=self.m,
            stage=self._stage,
            message=self._message,
        )
        self._thread.start()

    def update(self, stage: str, message: str = "") -> None:
        with self._lock:
            self._stage = str(stage)
            self._message = str(message or "")

        # immediate write (in addition to periodic)
        upsert_case_status(
            self.config_path,
            run_id=self.run_id,
            cell=self.cell,
            n=self.n,
            m=self.m,
            stage=self._stage,
            message=self._message,
        )

    def finish(self, ok: bool, message: str = "") -> None:
        with self._lock:
            self._stage = "done" if ok else "error"
            self._message = str(message or "")

        upsert_case_status(
            self.config_path,
            run_id=self.run_id,
            cell=self.cell,
            n=self.n,
            m=self.m,
            stage=self._stage,
            message=self._message,
            finished=True,
            ok=bool(ok),
        )
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.interval_seconds)

            with self._lock:
                stage = self._stage
                msg = self._message

            # heartbeat write
            upsert_case_status(
                self.config_path,
                run_id=self.run_id,
                cell=self.cell,
                n=self.n,
                m=self.m,
                stage=stage,
                message=msg,
            )

# ============================================================
# CONFIG + STATE
# ============================================================

def _read_config(config_path: str | Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
    config_path = Path(config_path)
    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding="utf-8")

    pg = cfg["postgres"]
    watch = cfg["watch"]

    connect_kwargs = dict(
        host=pg.get("host"),
        port=pg.getint("port", fallback=5432),
        dbname=pg.get("dbname"),
        user=pg.get("user"),
        password=pg.get("password"),
        sslmode=pg.get("sslmode", fallback="require"),
    )

    watch_dict = dict(
        schema=watch.get("schema"),
        poll_seconds=watch.getint("poll_seconds", fallback=5),
        stable_check_seconds=watch.getint("stable_check_seconds", fallback=5),
        stable_rounds=watch.getint("stable_rounds", fallback=3),
        download_folder=watch.get("download_folder", fallback="Downloaded"),
        include_header=watch.getboolean("include_header", fallback=False),
        state_file=watch.get("state_file", fallback=".table_state.json"),
    )

    return connect_kwargs, watch_dict


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"processed": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path, processed: Set[str]) -> None:
    path.write_text(json.dumps({"processed": sorted(processed)}, indent=2), encoding="utf-8")


# ============================================================
# DB HELPERS
# ============================================================

def _fetch_tables(conn: psycopg.Connection, schema: str) -> Set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
            """,
            (schema,),
        )
        return {r[0] for r in cur.fetchall()}


def _count_rows(conn: psycopg.Connection, schema: str, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}.{}")
                    .format(sql.Identifier(schema), sql.Identifier(table)))
        return cur.fetchone()[0]


def _wait_until_stable(
    conn: psycopg.Connection,
    *,
    schema: str,
    tables: List[str],
    stable_check_seconds: int,
    stable_rounds: int,
) -> None:
    last = None
    stable = 0
    while stable < stable_rounds:
        counts = tuple(_count_rows(conn, schema, t) for t in tables)
        if counts == last:
            stable += 1
        else:
            stable = 1
            last = counts
        if stable < stable_rounds:
            time.sleep(stable_check_seconds)


def _download_csv(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    out: Path,
    include_header: bool,
) -> None:
    copy_sql = f'COPY "{schema}"."{table}" TO STDOUT WITH (FORMAT csv'
    if include_header:
        copy_sql += ", HEADER true"
    copy_sql += ")"

    with conn.cursor() as cur, cur.copy(copy_sql) as cp:
        out.write_text(
            "".join(chunk.tobytes().decode("utf-8") for chunk in cp),
            encoding="utf-8",
        )


def _download_metadata_raw(
    conn: psycopg.Connection,
    schema: str,
    table: str,
    out: Path,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT raw_text FROM {}.{} ORDER BY id DESC LIMIT 1")
            .format(sql.Identifier(schema), sql.Identifier(table))
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Metadata table {table} is empty")
        out.write_text(row[0], encoding="utf-8")


# ============================================================
# TABLE NAME PATTERNS (NEW FORMAT)
# ============================================================

RE_EIS   = re.compile(r"^eis(?P<cell>\d+)cycle(?P<n>\d+)soc(?P<m>\d+)$", re.I)
RE_CYCLE = re.compile(r"^cycling(?P<cell>\d+)cycle(?P<n>\d+)soc(?P<m>\d+)$", re.I)
RE_META  = re.compile(r"^metadata(?P<cell>\d+)cycle(?P<n>\d+)soc(?P<m>\d+)$", re.I)

EIS, CYCLE, META = "eis", "cycle", "meta"


def _classify_tables(tables: Set[str]) -> Dict[Tuple[int, int, int], Set[str]]:
    out: Dict[Tuple[int, int, int], Set[str]] = {}
    for t in tables:
        for kind, rx in [(EIS, RE_EIS), (CYCLE, RE_CYCLE), (META, RE_META)]:
            m = rx.match(t)
            if m:
                key = (int(m["cell"]), int(m["n"]), int(m["m"]))
                out.setdefault(key, set()).add(kind)
    return out


def _key(cell: int, n: int, m: int) -> str:
    return f"cell={cell},n={n},m={m}"


# ============================================================
# TRIO OBJECT
# ============================================================

@dataclass(frozen=True)
class TrioDownload:
    cell: int
    n: int
    m: int
    eis_csv: Path
    cycle_csv: Path
    meta_txt: Path


# ============================================================
# WATCH + DOWNLOAD API
# ============================================================

def wait_for_next_trio_and_download(
    config_path: str | Path,
    *,
    folder: Optional[str | Path] = None,
) -> TrioDownload:

    connect_kwargs, watch = _read_config(config_path)
    schema = watch["schema"]

    state_file = Path(watch["state_file"])
    state = _load_state(state_file)
    processed: Set[str] = set(state.get("processed", []))

    folder = Path(folder or watch["download_folder"])
    folder.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(**connect_kwargs) as conn:
        while True:
            tables = _fetch_tables(conn, schema)
            classified = _classify_tables(tables)

            for (cell, n, m), kinds in classified.items():
                if {EIS, CYCLE, META}.issubset(kinds):
                    key = _key(cell, n, m)
                    if key in processed:
                        continue

                    eis = f"eis{cell}cycle{n}soc{m}"
                    cyc = f"cycling{cell}cycle{n}soc{m}"
                    met = f"metadata{cell}cycle{n}soc{m}"

                    _wait_until_stable(
                        conn,
                        schema=schema,
                        tables=[eis, cyc, met],
                        stable_check_seconds=watch["stable_check_seconds"],
                        stable_rounds=watch["stable_rounds"],
                    )

                    eis_csv = folder / f"{eis}.csv"
                    cycle_csv = folder / f"{cyc}.csv"
                    meta_txt = folder / f"{met}.txt"

                    _download_csv(conn, schema, eis, eis_csv, watch["include_header"])
                    _download_csv(conn, schema, cyc, cycle_csv, watch["include_header"])
                    _download_metadata_raw(conn, schema, met, meta_txt)

                    processed.add(key)
                    _save_state(state_file, processed)

                    return TrioDownload(cell, n, m, eis_csv, cycle_csv, meta_txt)

            time.sleep(watch["poll_seconds"])


# ============================================================
# UPLOAD HELPERS (RESTORED)
# ============================================================

def _sanitize_ident(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", name.lower())
    if not re.match(r"^[a-z_][a-z0-9_]*$", s):
        raise ValueError(f"Unsafe identifier: {name}")
    return s


def _ensure_schema(conn: psycopg.Connection, schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}")
                    .format(sql.Identifier(schema)))


def _upload_dataframe_as_table(
    conn: psycopg.Connection,
    *,
    schema: str,
    table: str,
    df: pd.DataFrame,
    if_exists: str,
) -> None:
    schema = _sanitize_ident(schema)
    table = _sanitize_ident(table)

    full = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))

    df = df.copy()
    df.columns = [_sanitize_ident(c) for c in df.columns]

    cols_sql = sql.SQL(", ").join(
        sql.SQL("{} TEXT").format(sql.Identifier(c)) for c in df.columns
    )

    with conn.cursor() as cur:
        if if_exists == "replace":
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(full))

        cur.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(full, cols_sql))

        buf = io.StringIO()
        df.to_csv(buf, index=False, header=True)
        buf.seek(0)

        copy_sql = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(
            full, sql.SQL(", ").join(sql.Identifier(c) for c in df.columns)
        )

        with cur.copy(copy_sql) as cp:
            cp.write(buf.getvalue().encode("utf-8"))


def _upload_dict_as_kv_table(
    conn: psycopg.Connection,
    *,
    schema: str,
    table: str,
    d: dict,
    if_exists: str,
) -> None:
    schema = _sanitize_ident(schema)
    table = _sanitize_ident(table)
    full = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))

    with conn.cursor() as cur:
        if if_exists == "replace":
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(full))

        cur.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} (key TEXT PRIMARY KEY, value TEXT)")
            .format(full)
        )

        rows = [(str(k), str(v)) for k, v in d.items()]
        cur.executemany(
            sql.SQL("INSERT INTO {} (key, value) VALUES (%s, %s)")
            .format(full),
            rows,
        )


def upload_comsol_outputs(
    config_path: str | Path,
    *,
    cell: int,
    n: int,
    m: int,
    eis_df: pd.DataFrame,
    socv_df: pd.DataFrame,
    out_dict: dict,
    if_exists: str = "replace",
) -> dict:

    connect_kwargs, watch = _read_config(config_path)
    schema = watch["schema"]

    eis_table = f"outputeis{cell}cycle{n}soc{m}"
    socv_table = f"outputcycling{cell}cycle{n}soc{m}"
    meta_table = f"outputmetadata{cell}cycle{n}soc{m}"

    with psycopg.connect(**connect_kwargs) as conn:
        with conn.transaction():
            _ensure_schema(conn, schema)
            _upload_dataframe_as_table(conn, schema=schema, table=eis_table, df=eis_df, if_exists=if_exists)
            _upload_dataframe_as_table(conn, schema=schema, table=socv_table, df=socv_df, if_exists=if_exists)
            _upload_dict_as_kv_table(conn, schema=schema, table=meta_table, d=out_dict, if_exists=if_exists)

    return {
        "schema": schema,
        "eis_table": f"{schema}.{_sanitize_ident(eis_table)}",
        "cycle_table": f"{schema}.{_sanitize_ident(socv_table)}",
        "metadata_table": f"{schema}.{_sanitize_ident(meta_table)}",
    }

