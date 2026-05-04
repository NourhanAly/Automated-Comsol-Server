import re
import mph
import os
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # no GUI; safe for batch runs
import matplotlib.pyplot as plt
import numpy as np
import jpype
from pathlib import Path
import json
import ast
import timeit
import ComsolClient
import Cycle


def ComsolFlow(work_path, Case_path, EIS_loc, eis, cycle, meta, cell, Cycle_no, target_SOC):
    start = timeit.default_timer()

    print(f"[PROCESS] Starting COMSOL flow for Cell={cell}, Cycle={Cycle_no}, SOC={target_SOC}")

    case_A = meta
    case_A["no_cycles"] = Cycle_no + 100
    # EIS Data & Potential Experimental data
    EIS_EXP = eis
    EIS_EXP.columns = ["freq_Hz", "Zre_ohm", "Zim_ohm"]

    VT_EXP = cycle
    VT_EXP.columns = ["StepTime(Seconds)", "Voltage(V)"]

    t = VT_EXP["StepTime(Seconds)"].to_numpy()
    print(f"[SOLVING] (Cell={cell}) Calculating time point for SOC")

    reset = np.r_[False, np.diff(t) < 0]
    dt = np.median(np.diff(t)[np.diff(t) > 0]) if np.any(np.diff(t) > 0) else 0.0

    seg_id = np.cumsum(reset)
    VT_EXP["time_unwrapped"] = VT_EXP["StepTime(Seconds)"].to_numpy().astype(float)
    offset = 0.0

    for s in np.unique(seg_id):
        idx = np.where(seg_id == s)[0]
        VT_EXP.loc[idx, "time_unwrapped"] += offset
        offset = VT_EXP.loc[idx, "time_unwrapped"].iloc[-1] + dt

    VT_EXP["StepTime(Seconds)"] = VT_EXP["time_unwrapped"]

    print(f"[PROCESS] (Cell={cell}) reading units file")
    units_path = work_path / "Units.txt"
    with open(units_path, "r", encoding="utf-8") as f:
        units_map = ast.literal_eval(f.read())

    # COMSOL step 1: First study
    print(f"[PROCESS] (Cell={cell}) opening model")
    name = work_path / 'NEMO_MODEL_N_49.mph'
    client, model = ComsolClient.load_model_with_retry(name, retry_seconds=120)

    study1_label = "Study1_time_inputtoEIS"
    print(f"[SOLUTION] (Cell={cell}) preparing the first study")
    print(f"[SOLUTION] (Cell={cell}) Parameters update")

    ComsolClient.apply_params(model, case_A)

    progress_file1 = ComsolClient.Call_log_next_to_model(name, study1_label)

    print(f"[SOLUTION] (Cell={cell}) Starting first study: {study1_label}")
    study1_tag, soltag1, dset1_node, res1 = ComsolClient.run_study_with_license_wait(
        model=model,
        study_label=study1_label,
        dset_tag="dset_std1_time_input",
        retry_seconds=120,
    )

    idx, Time = model.inner(dataset=dset1_node)
    SOC = model.evaluate("liion.SOC_cell", dataset=dset1_node)
    print(f"[SOLUTION] (Cell={cell}) storing first study results")

    STD1OUT = pd.DataFrame({'Time': Time, 'State of charge': SOC})
    STD1OUT.to_csv(Case_path / f"Cell_{cell}_FirstStudyOUT.csv", index=False)

    T_acc = Cycle.time_at_soc_for_cycle_discharge(
        Time, SOC,
        target_SOC / 100.0,
        Cycle_no // 100,     # 0,1,2,...
        min_soc_drop=0.005,
        min_points=2,
        eps=1e-6,
        relative_time=False
    )

    print(f"[SOLUTION] (Cell={cell}) Time = {T_acc}")

    mid = timeit.default_timer()
    print(f"[DEBUG] (Cell={cell}) Time for first study: {mid - start} Seconds")

    # COMSOL step 2: Second Study
    print(f"[SOLUTION] (Cell={cell}) preparing the second study")
    Case_B = {"TimeSt": T_acc}
    Case_B["no_cycles"] = Cycle_no + 100
    print(f"[SOLUTION] (Cell={cell}) Parameters update")
    ComsolClient.apply_params(model, Case_B)

    print(f"[SOLUTION] (Cell={cell}) Uploading optimization file")
    ParEst = model.java.study("std5").feature('lsqo')
    ParEst.set("source", "file")
    ParEst.set("filename", str(EIS_loc))

    study2_label = 'Study2-EIS_PARAMETRESTIMATION'
    progress_file2 = ComsolClient.Call_log_next_to_model(name, study2_label)

    print(f"[SOLUTION] (Cell={cell}) Starting second study: {study2_label}")
    study2_tag, soltag2, dset2_node, res2 = ComsolClient.run_study_with_license_wait(
        model=model,
        study_label=study2_label,
        dset_tag="dset_std2_EIS_Output",
        retry_seconds=120,
    )

    dset2_tag = "dset_std2_EIS_Output"
    point_id = 4
    pev_tag = "pev_geo4"
    num = res2.numerical()
    num.create(pev_tag, "EvalPoint")

    pev = res2.numerical(pev_tag)
    pev.set("data", dset2_tag)
    pev.selection().set([point_id])
    pev.set("expr", ["freq", "real(comp1.Z_ref_NMC)", "-imag(comp1.Z_ref_NMC)"])
    pev.run()

    freq, real, imag = np.array(pev.getReal(), dtype=float)
    print(f"[SOLUTION] (Cell={cell}) EIS data extracted")

    EIS_out = pd.DataFrame({'Frequency': freq, 'Real': real, 'Imaginary': imag})
    EIS_out.to_csv(Case_path / f"Cell_{cell}_EIS_model.csv", index=False)

    plt.figure()
    plt.plot(EIS_EXP["Zre_ohm"], EIS_EXP["Zim_ohm"], marker="o", linestyle="-", label='Experimental')
    plt.plot(real, imag, marker="x", linestyle="-", label='Model')
    plt.xlabel("Z' (Real)")
    plt.ylabel("-Z'' (-Imag)")
    plt.legend()
    plt.grid(True)
    plt.savefig(Case_path / f"Cell_{cell}_EIS_OptvsEXP.png")
    plt.close()
    tbls2 = res2.table()
    tags = [str(t) for t in tbls2.tags()]
    tbl_tag = tags[0]

    tbl = res2.table(tbl_tag)
    headers = list(tbl.getColumnHeaders())
    data = np.array(tbl.getReal(), dtype=float)
    df = pd.DataFrame(data, columns=headers)

    df.columns = [str(c) for c in df.columns]
    df.columns = [re.sub(r"\s*\([^)]*\)\s*$", "", c).strip() for c in df.columns]

    final = df.iloc[-1]
    out = {}
    for name, unit in units_map.items():
        if name in df.columns:
            val = final[name]
            if isinstance(val, (float, np.floating)):
                val_str = f"{val:.4g}"
            else:
                val_str = str(val)
            unit = str(unit).strip()
            out[name] = val_str + unit

    out_path = Case_path / f"Cell_{cell}_output2nd.txt"
    print(f"[SOLUTION] (Cell={cell}) optimized parameters stored in: {out_path}")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=4)

    mid2 = timeit.default_timer()
    print(f"[DEBUG] (Cell={cell}) Time for second study: {mid2 - mid} Seconds")

    # COMSOL step 3: rerun with new parameters
    out.popitem()  # keeping your existing logic as-is
    print(f"[SOLUTION] (Cell={cell}) preparing the third study")

    ComsolClient.apply_params(model, out)

    study3_label = "Study1_time_inputtoEIS"
    progress_file3 = ComsolClient.Call_log_next_to_model(name, study3_label)

    print(f"[SOLUTION] (Cell={cell}) Starting third study: {study3_label}")
    study3_tag, soltag3, dset3_node, res3 = ComsolClient.run_study_with_license_wait(
        model=model,
        study_label=study3_label,
        dset_tag="dset_std1_time_input",
        retry_seconds=120,
    )

    idx2, Time2 = model.inner(dataset=dset3_node)
    Potential2 = model.evaluate("E_cell", dataset=dset3_node)
    print(f"[SOLUTION] (Cell={cell}) SOC data extracted")

    pot_out = pd.DataFrame({'Time': Time2, 'Voltage': Potential2})
    pot_out.to_csv(Case_path / f"Cell_{cell}_SOC(V)_model.csv", index=False)

    plt.figure()
    plt.plot(VT_EXP["StepTime(Seconds)"], VT_EXP["Voltage(V)"], marker="o", linestyle="-", label='Experimental')
    plt.plot(Time2, Potential2, marker="x", linestyle="-", label='Model')
    plt.xlabel("Time (seconds)")
    plt.ylabel("Voltage (V)")
    plt.legend()
    plt.grid(True)
    plt.savefig(Case_path / f'Cell_{cell}_Potential2_OptvsEXP.png')
    plt.close()
    mid3 = timeit.default_timer()
    print(f"[SOLUTION] (Cell={cell}) Time for third study: {mid3 - mid2} Seconds")

    final = timeit.default_timer()
    print(f"[DEBUG] (Cell={cell}) Total study time: {final - start} Seconds")
    print(f"[END] (Cell={cell}) Model flow completed")
        # ===============================
    # ALWAYS close COMSOL here
    # ===============================
    #print(f"[CLEANUP] (Cell={cell}) Closing COMSOL session/model")

    #try:
        #client.remove(model)      # unload model from COMSOL session
    #except Exception as e:
        #print("[CLEANUP] client.remove(model) failed:", e)

    #try:
        #client.disconnect()       # close COMSOL session
    #except Exception as e:
        #print("[CLEANUP] client.disconnect() failed:", e)

    return (EIS_out, pot_out, out)

