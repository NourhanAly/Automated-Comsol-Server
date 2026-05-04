import re
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # no GUI; safe for batch runs
import matplotlib.pyplot as plt
import numpy as np
import jpype
from pathlib import Path
import time
import mph

def apply_params(model, params: dict[str, object], *, strict=True):
    existing = model.parameters()  # dict of current params :contentReference[oaicite:3]{index=3}

    if strict:
        unknown = set(params) - set(existing)
        if unknown:
            raise KeyError(f"Unknown model parameter(s): {sorted(unknown)}")

    old = {k: existing.get(k) for k in params}  # keep for logging/rollback
    for name, value in params.items():
        # For units / expressions, pass strings like "1[mm]" or "1e-3[m]"
        # For complex values, also pass as string (e.g. "1+i") rather than Python complex. :contentReference[oaicite:4]{index=4}
        model.parameter(name, value)  

    return old


def load_model_with_retry(model_path: Path, *, retry_seconds: int = 120):
    while True:
        client = None
        try:
            client = mph.start()
            model = client.load(model_path)
            return client, model

        except Exception as e:
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass

            if is_retryable_comsol_error(e):
                print(f"\n[COMSOL] Transient error while loading model. Waiting {retry_seconds}s...", flush=True)
                time.sleep(retry_seconds)
                print("[COMSOL] Retrying model load...", flush=True)
                continue
            raise

def run_study_with_license_wait(
    *,
    model,
    study_label: str,
    dset_tag: str,
    retry_seconds: int = 120,
):
    """
    Runs a COMSOL study. If license error happens, waits and retries *at this study*.
    Does NOT reload the model (caller keeps the same model state).
    """
    while True:
        try:
            return run_study_and_get_solution(
                model,
                study_label=study_label,
                dset_tag=dset_tag,
            )
        except Exception as e:
            if is_license_error(e):
                print(f"\n[LICENSE] No license for '{study_label}'. Waiting {retry_seconds}s...", flush=True)
                time.sleep(retry_seconds)
                print(f"[LICENSE] Retrying study '{study_label}'...", flush=True)
                continue
            raise

def is_license_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    keywords = [
        "license", "licence", "no licenses", "no licence",
        "flexnet", "lmgrd", "checkout", "feature", "denied"
    ]
    return any(k in msg for k in keywords)

def is_retryable_comsol_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return is_license_error(exc) or ("not connected to a server" in msg)

def Call_log_next_to_model(model_path_str, studyname):
    ModelUtil = jpype.JClass("com.comsol.model.util.ModelUtil")

    model_path = Path(model_path_str)
    progress_file = model_path.with_suffix("")   
    progress_file = progress_file.parent / f"{studyname}_progress.txt"
    progress_file.parent.mkdir(parents=True, exist_ok=True)

    ModelUtil.showProgress(True)
    ModelUtil.showProgress(str(progress_file))
    ModelUtil.serverLog(f"Starting {studyname} from Python")

    return progress_file

def study_tag_from_label(model, study_label: str) -> str:
    """Resolve a COMSOL study label (GUI name) to its tag (e.g., std14)."""
    study_label = str(study_label)
    slist = model.java.study()  

    # exact match first
    for t in slist.tags():
        tag = str(t)
        if str(model.java.study(tag).label()) == study_label:
    
            return tag

    # fallback: substring match
    for t in slist.tags():
        tag = str(t)
        if study_label in str(model.java.study(tag).label()):
            return tag
    raise RuntimeError(f"Study label not found: '{study_label}'")


def latest_nonempty_solution_for_study_tag(model, study_tag: str) -> str:
    """Return highest-numbered non-empty sol#### belonging to the given study tag."""
    study_tag = str(study_tag)
    best_n, best_sol = None, None

    for soltag_obj in model.java.sol().tags():
        soltag = str(soltag_obj)
        sol = model.java.sol(soltag)

        if str(sol.study()) != study_tag:
            continue
        if sol.isEmpty():
            continue

        m = re.fullmatch(r"sol(\d+)", soltag)
        if not m:
            continue

        n = int(m.group(1))
        if best_n is None or n > best_n:
            best_n, best_sol = n, soltag

    if best_sol is None:
        raise RuntimeError(f"No non-empty sol* found for study tag '{study_tag}'")

    return best_sol


def ensure_solution_dataset_node(model, dset_tag: str, soltag: str):
    """
    Create/update a Results->Dataset of type 'Solution' with tag dset_tag,
    point it to soltag, and return the mph Node for it.
    """
    dset_tag = str(dset_tag)
    soltag = str(soltag)

    
    if model.java.sol(soltag).isEmpty():
        raise RuntimeError(f"{soltag} is empty; cannot point dataset to it.")

    res = model.java.result()
    dsets = res.dataset()

    existing = [str(x) for x in dsets.tags()]

    if dset_tag not in existing:
        dsets.create(dset_tag, "Solution")
    
    res.dataset(dset_tag).set("solution", soltag)
    # Return mph Node (most reliable for model.inner/evaluate)
    dset_node = next(n for n in (model/'datasets').children() if n.tag() == dset_tag)
    return dset_node , res


def run_study_and_get_solution(model, study_label: str, dset_tag: str = "dset_auto"):
    """
    Run a study by LABEL and return (study_tag, soltag, dset_node).
    """ 
    study_tag = study_tag_from_label(model, study_label)

    # Run the whole study sequence as configured in COMSOL
    model.java.study(study_tag).run()

    # Find latest NON-empty solution produced by this study
    soltag = latest_nonempty_solution_for_study_tag(model, study_tag)
    
    
    # Make a stable dataset pointing to that solution
    dset_node, res = ensure_solution_dataset_node(model, dset_tag, soltag)
    return study_tag, soltag, dset_node , res
