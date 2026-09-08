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
        bp = pd.to_numeric(d.get("BELTS_PER_BOX"), errors="coerce") if "BELTS_PER_BOX" in d else pd.Series(np.nan, index=d.index)
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


def solve_day(mto: pd.DataFrame, slots: pd.DataFrame, date, time_limit: float = 30):
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
    weights = dict(zip(cm.COMPANY_ID.astype(str), cm.PRIORITY_WEIGHT))
    orders["PRIORITY_WEIGHT"] = orders.COMPANY_ID.astype(str).map(weights).fillna(1)

    O, L = len(orders), len(slots)
    # q[o,l] = integer boxes from order o stored at Process-4 pallet position l.
    # h[o] = boxes held outside the selected area. y[o,l] activates an order/location pair.
    q0 = 0
    h0 = O * L
    y0 = h0 + O
    N = y0 + O * L
    upper = np.r_[np.full(O * L, 32.0), orders.BOXES.to_numpy(float), np.ones(O * L)]
    bounds = Bounds(np.zeros(N), upper)
    integrality = np.ones(N)
    rows, lo, hi = [], [], []

    for o in range(O):
        rows.append({q0 + o * L + l: 1.0 for l in range(L)} | {h0 + o: 1.0})
        lo.append(float(orders.iloc[o].BOXES)); hi.append(float(orders.iloc[o].BOXES))
    for l in range(L):
        rows.append({q0 + o * L + l: 1.0 for o in range(O)})
        lo.append(-np.inf); hi.append(32.0)
    for o in range(O):
        for l in range(L):
            rows.append({q0 + o * L + l: 1.0, y0 + o * L + l: -32.0})
            lo.append(-np.inf); hi.append(0.0)

    A = lil_matrix((len(rows), N), dtype=float)
    for r, row in enumerate(rows):
        for j, v in row.items():
            A[r, j] = v
    base = LinearConstraint(A.tocsr(), np.array(lo), np.array(hi))

    def run(c, extra=None):
        constraints = [base] + (extra or [])
        return milp(c, integrality=integrality, bounds=bounds, constraints=constraints, options={"time_limit": time_limit, "mip_rel_gap": 0.0})

    # Stage 1: maximise priority-company boxes stored.
    c1 = np.zeros(N)
    for o in range(O):
        c1[q0 + o * L:q0 + (o + 1) * L] = -orders.iloc[o].PRIORITY_WEIGHT
    r1 = run(c1)
    if r1.x is None:
        raise RuntimeError(f"MILP stage 1 failed: {r1.message}")
    p_opt = round(float(sum(r1.x[q0 + o * L:q0 + (o + 1) * L].sum() * orders.iloc[o].PRIORITY_WEIGHT for o in range(O))), 6)
    P = lil_matrix((1, N))
    for o in range(O):
        for l in range(L):
            P[0, q0 + o * L + l] = orders.iloc[o].PRIORITY_WEIGHT
    pcon = LinearConstraint(P.tocsr(), np.array([p_opt]), np.array([p_opt]))

    # Stage 2: maximise total boxes while preserving Stage 1.
    c2 = np.zeros(N); c2[q0:q0 + O * L] = -1
    r2 = run(c2, [pcon])
    if r2.x is None:
        raise RuntimeError(f"MILP stage 2 failed: {r2.message}")
    total_opt = round(float(r2.x[q0:q0 + O * L].sum()), 6)
    T = lil_matrix((1, N)); T[0, q0:q0 + O * L] = 1
    tcon = LinearConstraint(T.tocsr(), np.array([total_opt]), np.array([total_opt]))

    # Stage 3: minimise retrieval distance and order-location fragmentation.
    dist = slots.DISTANCE_FROM_DOOR_M.to_numpy(float)
    c3 = np.zeros(N)
    for o in range(O):
        w = orders.iloc[o].PRIORITY_WEIGHT
        c3[q0 + o * L:q0 + (o + 1) * L] = dist * w
        c3[y0 + o * L:y0 + (o + 1) * L] = 8.0
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
                    "INVOICE_ID": orders.iloc[o].INVOICE_ID,
                    "COMPANY_ID": orders.iloc[o].COMPANY_ID,
                    "BOXES_STORED": q,
                    "SLOT_ID": slots.iloc[l].SLOT_ID,
                    "X_M": slots.iloc[l].X_M,
                    "Y_M": slots.iloc[l].Y_M,
                    "DISTANCE_FROM_DOOR_M": slots.iloc[l].DISTANCE_FROM_DOOR_M,
                    "PRIORITY_WEIGHT": orders.iloc[o].PRIORITY_WEIGHT,
                })
        h = int(round(X[h0 + o]))
        if h:
            held.append({"INVOICE_ID": orders.iloc[o].INVOICE_ID, "COMPANY_ID": orders.iloc[o].COMPANY_ID, "BOXES_HELD": h, "PRIORITY_WEIGHT": orders.iloc[o].PRIORITY_WEIGHT})

    alloc = pd.DataFrame(alloc)
    held = pd.DataFrame(held)
    if alloc.empty:
        alloc = pd.DataFrame(columns=["INVOICE_ID", "COMPANY_ID", "BOXES_STORED", "SLOT_ID", "X_M", "Y_M", "DISTANCE_FROM_DOOR_M", "PRIORITY_WEIGHT"])
    if held.empty:
        held = pd.DataFrame(columns=["INVOICE_ID", "COMPANY_ID", "BOXES_HELD", "PRIORITY_WEIGHT"])

    loc = alloc.groupby("SLOT_ID", as_index=False).agg(
        BOXES_STORED=("BOXES_STORED", "sum"), ORDERS=("INVOICE_ID", "nunique"), COMPANIES=("COMPANY_ID", "nunique"), DISTANCE_FROM_DOOR_M=("DISTANCE_FROM_DOOR_M", "first")
    )
    if not loc.empty:
        loc["UTILISATION_PCT"] = loc.BOXES_STORED / 32 * 100

    order_skus = day.groupby("INVOICE_ID").ITEM_SIZE.agg(lambda s: ", ".join(sorted(set(s.astype(str))))).to_dict()
    if not alloc.empty:
        alloc["SKU_LIST_IN_ORDER"] = alloc.INVOICE_ID.map(order_skus)

    summary = {
        "Date": date.date().isoformat(),
        "Process4_pallet_positions": L,
        "MTO_boxes": int(day.BOXES.sum()),
        "Required_pallet_equivalent": int(np.ceil(day.BOXES.sum() / 32)),
        "Boxes_stored": int(alloc.BOXES_STORED.sum()),
        "Boxes_held": int(held.BOXES_HELD.sum()) if not held.empty else 0,
        "Storage_utilisation_pct": float(alloc.BOXES_STORED.sum() / (L * 32) * 100),
        "Used_pallet_positions": int(loc.shape[0]),
        "Priority_boxes_stored": int(sum(r.BOXES_STORED for _, r in alloc.iterrows() if r.PRIORITY_WEIGHT > 1)),
        "Status": r3.message,
    }
    return {
        "summary": summary,
        "allocation": alloc,
        "held": held,
        "location_summary": loc,
        "orders": orders,
        "company_master": cm,
        "sku_master": sku_master(d),
        "candidate_groups": sku_cooccurrence(d),
        "solver_messages": [r1.message, r2.message, r3.message],
    }
