from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

BOXES_PER_PALLET = 32
FALLBACK_BELTS_PER_BOX = 28


def clean_mto(mto: pd.DataFrame) -> pd.DataFrame:
    d = mto.copy()
    required = {"DATE", "ITEM_SIZE", "BELTS", "INVOICE_ID", "COMPANY_ID"}
    missing = required - set(d.columns)
    if missing:
        raise ValueError(f"MTO data missing: {sorted(missing)}")
    d["DATE"] = pd.to_datetime(d["DATE"], errors="coerce")
    d["ITEM_SIZE"] = d["ITEM_SIZE"].astype("string").str.strip().str.upper()
    d["BELTS"] = pd.to_numeric(d["BELTS"], errors="coerce")
    d["BOXES"] = pd.to_numeric(d.get("BOXES"), errors="coerce")
    if d["BOXES"].isna().any():
        bp = pd.to_numeric(d["BELTS_PER_BOX"], errors="coerce") if "BELTS_PER_BOX" in d else pd.Series(np.nan, index=d.index)
        d["BOXES"] = d["BOXES"].fillna(np.ceil(d["BELTS"] / bp.fillna(FALLBACK_BELTS_PER_BOX)))
    d = d.dropna(subset=["DATE", "ITEM_SIZE", "BELTS", "BOXES", "INVOICE_ID", "COMPANY_ID"])
    d = d[(d.BELTS > 0) & (d.BOXES > 0)].copy()
    d["BOXES"] = np.ceil(d.BOXES).astype(int)
    d["COMPANY_ID"] = d.COMPANY_ID.astype(str)
    d["INVOICE_ID"] = d.INVOICE_ID.astype(str)
    return d


def company_master(mto: pd.DataFrame, threshold: float = 0.80) -> pd.DataFrame:
    d = clean_mto(mto)
    if "COMPANY_NAME" not in d:
        d["COMPANY_NAME"] = "Unknown"
    d["COMPANY_NAME"] = d.COMPANY_NAME.astype("string").str.strip()

    def canonical(s):
        s = s[(s.notna()) & (~s.str.upper().isin(["NAN", "NONE", "NULL", ""]))]
        return s.mode().iloc[0] if not s.empty else "Unknown"

    names = d.groupby("COMPANY_ID").COMPANY_NAME.agg(canonical)
    g = d.groupby("COMPANY_ID", as_index=False).agg(
        MTO_LINES=("ITEM_SIZE", "size"),
        MTO_BELTS=("BELTS", "sum"),
        MTO_BOXES=("BOXES", "sum"),
        MTO_ORDERS=("INVOICE_ID", "nunique"),
        ACTIVE_DAYS=("DATE", "nunique"),
    )
    g["COMPANY_NAME"] = g.COMPANY_ID.map(names).fillna("Unknown")
    g = g.sort_values(["MTO_BOXES", "MTO_BELTS"], ascending=False).reset_index(drop=True)
    total = g.MTO_BOXES.sum()
    g["VOLUME_SHARE_PCT"] = g.MTO_BOXES / total * 100 if total else 0
    g["CUMULATIVE_SHARE_PCT"] = g.VOLUME_SHARE_PCT.cumsum()
    k = int(np.searchsorted(g.CUMULATIVE_SHARE_PCT.to_numpy(), threshold * 100, side="left")) if len(g) else -1
    g["PRIORITY"] = False
    if k >= 0:
        g.loc[:k, "PRIORITY"] = True
    g["PRIORITY_WEIGHT"] = np.where(g.PRIORITY, 3.0, 1.0)
    return g


def sku_master(mto: pd.DataFrame) -> pd.DataFrame:
    d = clean_mto(mto)
    s = d.groupby("ITEM_SIZE", as_index=False).agg(
        MTO_LINES=("ITEM_SIZE", "size"),
        MTO_BELTS=("BELTS", "sum"),
        MTO_BOXES=("BOXES", "sum"),
        ACTIVE_DAYS=("DATE", "nunique"),
        UNIQUE_ORDERS=("INVOICE_ID", "nunique"),
    )
    s["FREQUENCY_SCORE"] = s.MTO_LINES.rank(pct=True)
    s["VOLUME_SCORE"] = s.MTO_BELTS.rank(pct=True)
    s["MOVEMENT_SCORE"] = 0.5 * s.FREQUENCY_SCORE + 0.5 * s.VOLUME_SCORE
    return s.sort_values(["MOVEMENT_SCORE", "MTO_LINES", "MTO_BELTS"], ascending=False).reset_index(drop=True)


def sku_cooccurrence(mto: pd.DataFrame, top_n: int = 100) -> pd.DataFrame:
    d = clean_mto(mto)
    rows = []
    for order, g in d.groupby("INVOICE_ID"):
        skus = sorted(set(g.ITEM_SIZE.astype(str)))
        for i, a in enumerate(skus):
            for b in skus[i + 1:]:
                rows.append((a, b, order))
    if not rows:
        return pd.DataFrame(columns=["SKU_A", "SKU_B", "SHARED_ORDERS"])
    p = pd.DataFrame(rows, columns=["SKU_A", "SKU_B", "ORDER"])
    return p.groupby(["SKU_A", "SKU_B"], as_index=False).agg(SHARED_ORDERS=("ORDER", "nunique")).sort_values("SHARED_ORDERS", ascending=False).head(top_n).reset_index(drop=True)


def solve_day(mto: pd.DataFrame, slots: pd.DataFrame, date):
    """Exact three-stage MTO allocation MILP over the existing Process-4 locations."""
    d = clean_mto(mto)
    date = pd.Timestamp(date)
    day = d[d.DATE.dt.normalize() == date.normalize()].copy()
    if day.empty:
        raise ValueError(f"No MTO data for {date.date()}")
    req = {"SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M"}
    if not req.issubset(slots.columns):
        raise ValueError(f"Process-4 table missing {sorted(req - set(slots.columns))}")
    slots = slots.copy().sort_values("DISTANCE_FROM_DOOR_M").reset_index(drop=True)

    orders = day.groupby(["INVOICE_ID", "COMPANY_ID"], as_index=False).agg(
        BOXES=("BOXES", "sum"), BELTS=("BELTS", "sum"), LINES=("ITEM_SIZE", "size"), SKUS=("ITEM_SIZE", "nunique")
    )
    cm = company_master(d)
    priority_map = dict(zip(cm.COMPANY_ID.astype(str), cm.PRIORITY))
    orders["PRIORITY"] = orders.COMPANY_ID.astype(str).map(priority_map).fillna(False).astype(bool)

    O, L = len(orders), len(slots)
    q0 = 0
    h0 = O * L
    N = h0 + O
    bounds = Bounds(np.zeros(N), np.r_[np.full(O * L, float(BOXES_PER_PALLET)), orders.BOXES.to_numpy(float)])
    integrality = np.ones(N)

    A = lil_matrix((O + L, N), dtype=float)
    lo, hi = [], []
    for o in range(O):
        for l in range(L):
            A[o, q0 + o * L + l] = 1.0
        A[o, h0 + o] = 1.0
        lo.append(float(orders.iloc[o].BOXES))
        hi.append(float(orders.iloc[o].BOXES))
    for l in range(L):
        for o in range(O):
            A[O + l, q0 + o * L + l] = 1.0
        lo.append(-np.inf)
        hi.append(float(BOXES_PER_PALLET))
    base = LinearConstraint(A.tocsr(), np.array(lo), np.array(hi))

    def run(c, extra=None):
        return milp(c, integrality=integrality, bounds=bounds, constraints=[base] + (extra or []), options={"mip_rel_gap": 0.0})

    c1 = np.zeros(N)
    for o in range(O):
        if orders.iloc[o].PRIORITY:
            c1[q0 + o * L:q0 + (o + 1) * L] = -1.0
    r1 = run(c1)
    if r1.x is None:
        raise RuntimeError(f"MILP stage 1 failed: {r1.message}")
    priority_opt = round(float(-r1.fun), 6)

    P = lil_matrix((1, N), dtype=float)
    for o in range(O):
        if orders.iloc[o].PRIORITY:
            for l in range(L):
                P[0, q0 + o * L + l] = 1.0
    pcon = LinearConstraint(P.tocsr(), np.array([priority_opt]), np.array([priority_opt]))

    c2 = np.zeros(N)
    c2[q0:q0 + O * L] = -1.0
    r2 = run(c2, [pcon])
    if r2.x is None:
        raise RuntimeError(f"MILP stage 2 failed: {r2.message}")
    total_opt = round(float(-r2.fun), 6)

    T = lil_matrix((1, N), dtype=float)
    for j in range(O * L):
        T[0, q0 + j] = 1.0
    tcon = LinearConstraint(T.tocsr(), np.array([total_opt]), np.array([total_opt]))

    c3 = np.zeros(N)
    dist = slots.DISTANCE_FROM_DOOR_M.to_numpy(float)
    for o in range(O):
        c3[q0 + o * L:q0 + (o + 1) * L] = dist
    r3 = run(c3, [pcon, tcon])
    if r3.x is None:
        raise RuntimeError(f"MILP stage 3 failed: {r3.message}")
    X = r3.x

    alloc, held = [], []
    for o in range(O):
        for l in range(L):
            q = int(round(X[q0 + o * L + l]))
            if q:
                alloc.append({
                    "Date": date.date().isoformat(),
                    "INVOICE_ID": orders.iloc[o].INVOICE_ID,
                    "COMPANY_ID": orders.iloc[o].COMPANY_ID,
                    "BOXES_STORED": q,
                    "SLOT_ID": slots.iloc[l].SLOT_ID,
                    "X_M": slots.iloc[l].X_M,
                    "Y_M": slots.iloc[l].Y_M,
                    "DISTANCE_FROM_DOOR_M": slots.iloc[l].DISTANCE_FROM_DOOR_M,
                    "PRIORITY": orders.iloc[o].PRIORITY,
                    "DISTANCE_BOX_M": q * float(slots.iloc[l].DISTANCE_FROM_DOOR_M),
                })
        h = int(round(X[h0 + o]))
        if h:
            held.append({
                "Date": date.date().isoformat(),
                "INVOICE_ID": orders.iloc[o].INVOICE_ID,
                "COMPANY_ID": orders.iloc[o].COMPANY_ID,
                "BOXES_HELD": h,
                "PRIORITY": orders.iloc[o].PRIORITY,
            })

    alloc = pd.DataFrame(alloc)
    held = pd.DataFrame(held)
    if alloc.empty:
        alloc = pd.DataFrame(columns=["Date", "INVOICE_ID", "COMPANY_ID", "BOXES_STORED", "SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M", "PRIORITY", "DISTANCE_BOX_M"])
    if held.empty:
        held = pd.DataFrame(columns=["Date", "INVOICE_ID", "COMPANY_ID", "BOXES_HELD", "PRIORITY"])

    loc = alloc.groupby("SLOT_ID", as_index=False).agg(
        BOXES_STORED=("BOXES_STORED", "sum"),
        ORDERS=("INVOICE_ID", "nunique"),
        COMPANIES=("COMPANY_ID", "nunique"),
        DISTANCE_FROM_DOOR_M=("DISTANCE_FROM_DOOR_M", "first"),
    )
    if not loc.empty:
        loc["UTILISATION_PCT"] = loc.BOXES_STORED / BOXES_PER_PALLET * 100

    stored = int(alloc.BOXES_STORED.sum())
    held_boxes = int(held.BOXES_HELD.sum()) if not held.empty else 0
    priority_required = int(orders.loc[orders.PRIORITY, "BOXES"].sum())
    priority_stored = int(alloc.loc[alloc.PRIORITY, "BOXES_STORED"].sum()) if not alloc.empty else 0
    total_distance = float(alloc.DISTANCE_BOX_M.sum()) if not alloc.empty else 0.0

    summary = {
        "Date": date.date().isoformat(),
        "Process4_pallet_positions": L,
        "MTO_boxes": int(day.BOXES.sum()),
        "Required_pallet_equivalent": int(np.ceil(day.BOXES.sum() / BOXES_PER_PALLET)),
        "Boxes_stored": stored,
        "Boxes_held": held_boxes,
        "Unused_box_capacity": int(L * BOXES_PER_PALLET - stored),
        "Storage_utilisation_pct": float(stored / (L * BOXES_PER_PALLET) * 100),
        "Used_pallet_positions": int(loc.shape[0]),
        "Priority_boxes_required": priority_required,
        "Priority_boxes_stored": priority_stored,
        "Priority_storage_rate_pct": float(priority_stored / priority_required * 100) if priority_required else 0.0,
        "Held_rate_pct": float(held_boxes / day.BOXES.sum() * 100) if day.BOXES.sum() else 0.0,
        "Total_Distance_Box_M": total_distance,
        "Weighted_Avg_Distance_M": float(total_distance / stored) if stored else 0.0,
        "Capacity_Gap_Pallets": max(int(np.ceil(day.BOXES.sum() / BOXES_PER_PALLET)) - L, 0),
        "Overflow": int(np.ceil(day.BOXES.sum() / BOXES_PER_PALLET)) > L,
        "Solver_status": r3.message,
    }
    return {
        "summary": summary,
        "allocation": alloc,
        "held": held,
        "location_summary": loc,
        "orders": orders,
        "company_master": cm,
        "sku_master": sku_master(d),
        "candidate_groups": sku_cooccurrence(d, 150),
        "solver_messages": [r1.message, r2.message, r3.message],
    }
