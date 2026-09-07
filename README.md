# Universal Warehouse Digital Twin

A reusable CAD-assisted warehouse digital twin for layout optimization, capacity planning, transaction-level MTO process analysis, WIP/storage trade-offs, and managerial decision support.

## Application workflow

1. Upload warehouse CAD and daily demand.
2. Review the CAD geometry and set the validated operating door.
3. Run **Model 1 — Physical Twin** for realistic/planning capacity and daily simulation.
4. Upload MTO transactions and the Size / Box Master.
5. Run **Model 2 — Process Twin** for handling time, touches, fatigue/error proxies, SKU-level WIP and temporary-storage requirements.
6. Use **Decision Workspace** to compare physical feasibility, process benefit and staging constraints.
7. Export the evidence workbooks when required.

## Design principles

- Model 1 and Model 2 share the same physical warehouse context.
- Observed data, master data, assumptions and modelled proxies are kept conceptually separate.
- Peak demand and temporary WIP are not incorrectly treated as the same physical capacity measure.
- The application is decision support, not an autonomous implementation approval.
- Technical evidence remains available, but is kept behind expandable sections so the main UI stays manager-facing.

## Repository structure

- `app.py` — main Streamlit application
- `core/` — CAD, layout, capacity, simulation, MTO process and export engines
- `tests/` — model tests
- `PROJECT_DOCUMENTATION.md` — project source-of-truth documentation
- `requirements.txt` — Python dependencies

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```
