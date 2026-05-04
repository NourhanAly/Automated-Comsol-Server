import subprocess
import sys
import importlib

# ── Package check ──────────────────────────────────────────────
REQUIRED_PACKAGES = {
    "pandas": "pandas",
    "matplotlib": "matplotlib",
    "numpy": "numpy",
    "jpype": "JPype1",     
    "mph": "mph",
    "psycopg": "psycopg[binary]",
}

def check_and_install_packages():
    missing = []
    for import_name, install_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(install_name)
    
    if missing:
        print(f"Missing packages detected: {missing}")
        print("Installing now...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--prefer-binary", *missing])
        print("All packages installed! Restarting may be required.\n")
    else:
        print("All packages verified ✓")

check_and_install_packages()



import DataBaseComm as DBC
from pathlib import Path
import shutil
import csv
import pandas as pd
from dataclasses import dataclass
from typing import Union
from SolutionFlowComsol import ComsolFlow
import time
import json


PathLike = Union[str, Path]


@dataclass(frozen=True)
class TrioData:
    eis: pd.DataFrame
    cycle: pd.DataFrame
    meta: dict[str, str]


def is_license_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    keywords = [
        "license", "licence", "no licenses", "no licence",
        "flexnet", "lmgrd", "checkout", "feature", "denied"
    ]
    return any(k in msg for k in keywords)


def run_comsol_until_license(
    *,
    path_root: Path,
    case_folder: Path,
    eis_dest: Path,
    eis_df: pd.DataFrame,
    cycle_df: pd.DataFrame,
    meta_dict: dict[str, str],
    cell: int,
    n: int,
    m: int,
    retry_seconds: int = 120,
    heartbeat: "DBC.CaseHeartbeat | None" = None,
):
    while True:
        try:
            if heartbeat:
                heartbeat.update("comsol", "Running COMSOL (license-wait enabled)")
            return ComsolFlow(path_root, case_folder, eis_dest, eis_df, cycle_df, meta_dict, cell, n, m)

        except Exception as e:
            if is_license_error(e):
                if heartbeat:
                    heartbeat.update(
                        "waiting_license",
                        f"No COMSOL license available. Waiting {retry_seconds}s then retry."
                    )
                print(
                    f"\n[LICENSE] No license available for Cell={cell}, Cycle={n}, SOC={m}. "
                    f"Waiting {retry_seconds}s...",
                    flush=True
                )
                time.sleep(retry_seconds)
                print("[LICENSE] Retrying now...", flush=True)
                continue

            # non-license error: just raise
            if heartbeat:
                heartbeat.update("comsol_error", f"{type(e).__name__}: {e}")
            raise


def _parse_metadata_file(meta_txt_path: Path, encoding: str = "utf-8") -> dict[str, str]:
    """
    NEW BEHAVIOR:
      - Metadata is stored as raw text in DB and written to .txt.
      - Try JSON first (most robust).
      - Fallback to your previous tab-delimited key/value parsing.
    """
    raw = meta_txt_path.read_text(encoding=encoding, errors="replace").strip()
    if not raw:
        return {}

    # 1) Try JSON
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return {str(k): "" if v is None else str(v) for k, v in obj.items()}
    except Exception:
        pass

    # 2) Fallback: tab-delimited key/value
    meta: dict[str, str] = {}
    with meta_txt_path.open("r", encoding=encoding, newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if not row:
                continue
            if len(row) < 2:
                continue
            k = (row[0] or "").strip()
            v = (row[1] or "").strip()
            if k:
                meta[k] = v
    return meta


def read_trio_files(
    eis_csv_path: PathLike,
    cycle_csv_path: PathLike,
    meta_txt_path: PathLike,
    *,
    eis_has_header: bool = False,
    cycle_has_header: bool = False,
    encoding: str = "utf-8",
) -> TrioData:
    eis_csv_path = Path(eis_csv_path)
    cycle_csv_path = Path(cycle_csv_path)
    meta_txt_path = Path(meta_txt_path)

    eis_df = pd.read_csv(eis_csv_path, header=0 if eis_has_header else None, encoding=encoding)
    cycle_df = pd.read_csv(cycle_csv_path, header=0 if cycle_has_header else None, encoding=encoding)

    meta = _parse_metadata_file(meta_txt_path, encoding=encoding)

    return TrioData(eis=eis_df, cycle=cycle_df, meta=meta)


def write_dict_pretty_txt(path: Path, d: dict) -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("{\n")
        items = list(d.items())
        for i, (k, v) in enumerate(items):
            k_str = esc(str(k))
            v_str = esc(str(v))
            comma = "," if i < len(items) - 1 else ""
            f.write(f'    "{k_str}": "{v_str}"{comma}\n')
        f.write("}\n")


def run_processing_flow(
    trio: DBC.TrioDownload,
    path_root: str | Path,
    download_root: str | Path,
    *,
    heartbeat: "DBC.CaseHeartbeat | None" = None,
):
    path_root = Path(path_root)
    case_root = path_root / "Cases"
    download_root = Path(download_root)

    if heartbeat:
        heartbeat.update("processing", "Preparing case folder and copying inputs")

    trio_eis = download_root / trio.eis_csv.name
    trio_cycle = download_root / trio.cycle_csv.name
    trio_meta = download_root / trio.meta_txt.name

    if not trio_eis.exists():
        raise FileNotFoundError(f"Missing EIS file in download folder: {trio_eis}")
    if not trio_cycle.exists():
        raise FileNotFoundError(f"Missing Cycle file in download folder: {trio_cycle}")
    if not trio_meta.exists():
        raise FileNotFoundError(f"Missing Meta file in download folder: {trio_meta}")

    case_folder = case_root / f"caseCell_{trio.cell}_Cycle_{trio.n}_SOC_{trio.m}"
    case_folder.mkdir(parents=True, exist_ok=True)
    print(f"[PROCESS] Created/using case folder: {case_folder.resolve()}", flush=True)

    eis_dest = case_folder / trio.eis_csv.name
    cycle_dest = case_folder / trio.cycle_csv.name
    meta_dest = case_folder / trio.meta_txt.name

    shutil.copy2(trio_eis, eis_dest)
    shutil.copy2(trio_cycle, cycle_dest)
    shutil.copy2(trio_meta, meta_dest)

    print("[PROCESS] Copied inputs to case folder:", flush=True)
    print(f"  EIS:   {eis_dest}", flush=True)
    print(f"  Cycle: {cycle_dest}", flush=True)
    print(f"  Meta:  {meta_dest}", flush=True)

    if heartbeat:
        heartbeat.update("processing", "Reading input CSV/TXT files")

    data = read_trio_files(
        eis_dest,
        cycle_dest,
        meta_dest,
        eis_has_header=False,
        cycle_has_header=False,
    )
    print("[PROCESS] Loaded data.", flush=True)

    # ONLY THIS retries on license (copies/reads do NOT repeat)
    EIS_out, pot_out, outDict = run_comsol_until_license(
        path_root=path_root,
        case_folder=case_folder,
        eis_dest=eis_dest,
        eis_df=data.eis,
        cycle_df=data.cycle,
        meta_dict=data.meta,
        cell=trio.cell,
        n=trio.n,
        m=trio.m,
        retry_seconds=120,
        heartbeat=heartbeat,
    )

    if heartbeat:
        heartbeat.update("processing", "COMSOL finished; returning outputs")

    return case_folder, EIS_out, pot_out, outDict


def main():
    time.sleep(20)
    Fileloc = Path.cwd()
    config_path = Fileloc / "db_watch.ini"
    download_folder = Fileloc / "Downloaded"

    print(f"[BOOT] CWD:      {Fileloc}")
    print(f"[BOOT] Config:   {config_path}")
    print(f"[BOOT] Download: {download_folder}")

    while True:
        # Wait for a trio (download step happens inside DBC.wait_for_next_trio_and_download)
        trio = DBC.wait_for_next_trio_and_download(
            config_path,
            folder=download_folder,
        )

        # ----------------------------
        # Heartbeat (10 minutes)
        # ----------------------------
        hb = DBC.CaseHeartbeat(
            config_path,
            cell=trio.cell,
            n=trio.n,
            m=trio.m,
            interval_seconds=600,  # 10 minutes
        )
        hb.start()
        hb.update("downloaded", "Trio downloaded; starting processing flow")

        try:
            case_folder, EIS_out, pot_out, outDict = run_processing_flow(
                trio,
                Fileloc,
                download_folder,
                heartbeat=hb,
            )

            hb.update("uploading", "Uploading COMSOL outputs to PostgreSQL")

            # FIX: pass cell=... (required)
            info = DBC.upload_comsol_outputs(
                config_path,
                cell=trio.cell,
                n=trio.n,
                m=trio.m,
                eis_df=EIS_out,
                socv_df=pot_out,
                out_dict=outDict,
                if_exists="replace",
            )

            hb.finish(True, "Upload completed successfully")
            print("[UPLOAD] Done:", info)

        except Exception as e:
            hb.finish(False, f"{type(e).__name__}: {e}")
            print(f"[ERROR] Processing/upload failed for Cell={trio.cell}, Cycle={trio.n}, SOC={trio.m}: {e}")
            continue


if __name__ == "__main__":
    main()
