import numpy as np

def _unwrap_time_if_needed(t: np.ndarray) -> np.ndarray:
    t = np.asarray(t, dtype=float)
    if t.size < 2:
        return t

    dt_all = np.diff(t)
    if not np.any(dt_all < 0):
        return t

    pos = dt_all[dt_all > 0]
    dt = float(np.median(pos)) if pos.size else 0.0

    reset = np.r_[False, dt_all < 0]
    seg_id = np.cumsum(reset)

    out = t.copy()

    prev_end = out[0]
    for s in np.unique(seg_id):
        idx = np.where(seg_id == s)[0]
        if s == 0:
            # first segment unchanged
            prev_end = out[idx][-1]
            continue

        seg_start = out[idx][0]
        shift = (prev_end + dt) - seg_start
        out[idx] += shift
        prev_end = out[idx][-1]

    return out


def find_discharge_segments(time, soc, *, min_soc_drop=0.005, min_points=2, eps=1e-10):
    """
    Find discharge segments as contiguous ranges where SOC decreases.
    Works with wrapped or unwrapped time.
    """
    t = np.asarray(time, dtype=float)
    s = np.asarray(soc, dtype=float)

    ok = np.isfinite(t) & np.isfinite(s)
    t, s = t[ok], s[ok]
    if t.size < 3:
        return []

    # Unwrap time if needed (handles resets)
    t = _unwrap_time_if_needed(t)

    ds = np.diff(s)
    is_discharge_step = ds < -eps

    segments = []
    n = len(s)
    i = 0
    while i < n - 1:
        if not is_discharge_step[i]:
            i += 1
            continue

        start = i
        while i < n - 1 and is_discharge_step[i]:
            i += 1
        end_step = i - 1

        seg_start = start
        seg_end = end_step + 1

        if (seg_end - seg_start + 1) < min_points:
            continue

        drop = s[seg_start] - s[seg_end]
        if drop < min_soc_drop:
            continue

        segments.append((seg_start, seg_end))

    return segments


def time_at_soc_for_cycle_discharge(time, soc, target_soc, cycle_i,
                                   *, min_soc_drop=0.005, min_points=2,
                                   eps=1e-10, relative_time=False,
                                   interpret_target_as_percent=True):
    """
    Return the time when SOC crosses target_soc during the discharge segment cycle_i.

    If there is no crossing, returns the time at which SOC is closest to target_soc
    (using interpolation within each interval), so it does NOT return NaN unless the
    segment is degenerate.

    cycle_i is 0-based over detected discharge segments (in the full series).

    If interpret_target_as_percent=True and target_soc > 1.5, treats target_soc as percent (100 -> 1.0).
    """
    t = np.asarray(time, dtype=float)
    s = np.asarray(soc, dtype=float)

    ok = np.isfinite(t) & np.isfinite(s)
    t, s = t[ok], s[ok]
    if t.size < 2:
        return np.nan

    # Unwrap time if needed
    t = _unwrap_time_if_needed(t)

    segments = find_discharge_segments(
        t, s,
        min_soc_drop=min_soc_drop,
        min_points=min_points,
        eps=eps
    )

    if cycle_i < 0 or cycle_i >= len(segments):
        raise ValueError(f"cycle_i={cycle_i} out of range. Found {len(segments)} discharge segments.")

    # Optional: accept "100" meaning 100%
    target = float(target_soc)
    if interpret_target_as_percent and target > 1.5:
        target = target / 100.0

    i0, i1 = segments[cycle_i]
    tc = t[i0:i1+1]
    sc = s[i0:i1+1]

    if tc.size < 2:
        return np.nan

    sc0, sc1 = sc[:-1], sc[1:]
    tc0, tc1 = tc[:-1], tc[1:]

    # 1) Try true downward crossing (discharge)
    idx = np.where((sc0 >= target) & (sc1 <= target))[0]
    if idx.size:
        k = idx[0]
        denom = (sc1[k] - sc0[k])
        if denom != 0:
            t_hit = tc0[k] + (target - sc0[k]) * (tc1[k] - tc0[k]) / denom
            return float(t_hit - tc[0]) if relative_time else float(t_hit)
        # if denom==0, fall through to closest-SOC

    # 2) No crossing -> return closest SOC time (interpolated within intervals)
    best_t = None
    best_err = np.inf

    for i in range(len(sc) - 1):
        sA, sB = sc[i], sc[i + 1]
        tA, tB = tc[i], tc[i + 1]

        dt = tB - tA
        if dt == 0:
            continue

        ds = sB - sA
        if ds == 0:
            # flat SOC in this interval
            s_clamp = sA
            t_clamp = tA
        else:
            # projection factor along interval where SOC would equal target
            r = (target - sA) / ds
            # clamp to [0,1] => closest point on this segment
            if r < 0.0:
                r = 0.0
            elif r > 1.0:
                r = 1.0
            t_clamp = tA + r * dt
            s_clamp = sA + r * ds

        err = abs(s_clamp - target)
        if err < best_err:
            best_err = err
            best_t = t_clamp

    if best_t is None:
        return np.nan

    return float(best_t - tc[0]) if relative_time else float(best_t)
