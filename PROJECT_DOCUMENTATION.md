# Universal Warehouse Digital Twin — Project Documentation

## 1. Project Identity

**Repository:** `Revanth015/SIP-project`

**Project:** Universal Warehouse Digital Twin

**Purpose:** Build a reusable, CAD-assisted warehouse Digital Twin that combines physical warehouse capacity/layout analysis with transaction-level process and WIP analysis so an operational manager can evaluate layout, capacity, storage and process decisions.

**Current application:** Streamlit application (`app.py`) with Model 1, Model 2, a Decision Workspace and download/export functions.

---

## 2. Core Project Concept

The project evolved from a company-specific warehouse study into a reusable Digital Twin framework.

The intended architecture is:

```text
Warehouse CAD + transaction/master data
              ↓
        Data validation
              ↓
       Physical Digital Twin
              ↓
Theoretical → Realistic → Planning capacity
              ↓
       Layout + slot simulation
              ↓
      Process / WIP Digital Twin
              ↓
 Current vs Proposed process
              ↓
 Storage paradox + operational trade-offs
              ↓
       Manager Decision Workspace
```

The Digital Twin is deliberately designed not to hard-code one company's capacity or layout. Warehouse geometry and operational assumptions determine the outputs.

---

## 3. Required Source Inputs

The target reusable system uses four principal source inputs:

1. **Warehouse diagram / CAD file — DXF**
2. **MTA transaction data**
3. **MTO transaction / packing-list data**
4. **Belt-size / Units-per-Box master table**

The project has also used a derived Daily Demand workbook during development and Model 1 testing. The final architecture should prefer deriving demand from the transaction data where possible rather than treating derived demand as a mandatory source.

### CAD

The CAD provides warehouse geometry. DXF semantics are not assumed to be perfect. The application therefore supports user-controlled operational inputs such as the operating door and designated pallet-storage area.

### MTO / MTA

Transactions are converted into standardized box quantities using the master table. Daily quantities and WIP behaviour are derived from the transaction records.

### Belt / Box Master

The master table is the preferred source for positive belt-per-box standards.

Important project rule:

- Master-table values of **0** must not be used as valid standards.
- If a SKU has no corresponding usable positive master value, the project uses the agreed fallback of **28 belts per box**.
- Conflicting positive master standards should be flagged rather than silently treated as valid.

This validation is important because box conversion directly affects storage and process calculations.

---

## 4. Model 1 — Physical Warehouse Digital Twin

### Objective

Model 1 represents the physical warehouse. It calculates how many pallet positions can theoretically fit, how many remain feasible after operational constraints, and what capacity should be used for normal planning.

### Main outputs

- Warehouse area
- Designated pallet-storage area
- Theoretical pallet capacity
- Realistic operational pallet capacity
- Planning capacity
- Target occupancy
- Pallet-slot coordinates
- Orientation
- Wall clearance
- Main aisle
- Cross aisle
- Turning requirement
- Average slot distance from operating door
- Daily occupancy
- Overflow days
- Daily simulation
- Layout alternatives

### Capacity definitions

**Theoretical capacity** is a geometric upper bound. It represents the maximum pallet-slot arrangement before the full set of operational constraints is applied.

**Realistic operational capacity** is the feasible number of slots after considering constraints such as wall clearance, aisles, turning/access requirements and obstacles.

**Planning capacity** is not physical capacity. It is:

`Planning Capacity = Realistic Capacity × Target Occupancy %`

The normal planning target is configurable and is intended to remain in the **60–70% range**, with **65%** as the default.

The model must not present 100% physical occupancy as a normal operating target.

---

## 5. Operating Door Logic

The operating door is intentionally **user-controlled**.

CAD-detected doors are reference candidates only. The application allows the manager/user to:

- enter operating-door X/Y coordinates, or
- use a detected CAD door as a starting point and then adjust it.

The selected operating door affects layout accessibility and travel-distance calculations.

This change was made because automatic door selection produced incorrect results for the project CAD.

---

## 6. Dedicated Pallet-Storage Area

A major practical enhancement is the ability to designate only part of the warehouse for pallet storage.

The system supports three modes:

### A. Automatic

Use the CAD-detected storage region.

### B. Full warehouse

Treat the entire warehouse as available for pallet storage.

### C. Manual rectangle

The manager defines the pallet-only storage area using X/Y minimum and maximum coordinates.

The selected region becomes the boundary for pallet capacity calculations.

### Operational rule

Pallet capacity, pallet placement and occupancy calculations must not use space outside the designated pallet-storage area.

This creates a practical managerial what-if capability:

`Storage area change → feasible capacity change → planning capacity change → occupancy/overflow change`

This is particularly important where the warehouse contains production, processing, movement, staging or other non-pallet activities.

---

## 7. Layout Logic

The layout engine evaluates pallet orientation and operational constraints rather than hard-coding a single layout.

Typical configurable parameters include:

- pallet width = 1.20 m
- pallet depth = 1.00 m
- wall clearance = 0.25 m default
- main aisle = 4.00 m default
- cross aisle = 2.00 m default
- turning diameter = 7.00 m default

The application can search feasible alternatives and compare capacity and average travel distance.

The manager can therefore evaluate trade-offs such as:

- higher slot count vs accessibility
- larger aisles vs capacity
- orientation vs travel distance
- dedicated storage area vs overflow risk

---

## 8. Daily Physical Simulation

Daily demand is mapped onto available pallet slots.

The simulation tracks:

- opening occupancy
- demand / pallet movement
- closing occupancy
- storage usage percentage
- overflow
- distance-related metrics
- time-related metrics
- fatigue proxy

The application can create a daily warehouse simulation GIF so the warehouse filling/emptying behaviour can be presented visually.

The simulation is intended as decision support, not a claim that every physical movement is measured exactly.

---

## 9. Model 2 — Process Digital Twin

Model 2 represents the transaction/process side of the warehouse.

It is designed to use the same physical warehouse context as Model 1 instead of creating an independent warehouse model.

### Shared Model 1 context

Model 2 inherits:

- warehouse geometry
- operating door
- pallet dimensions
- aisle constraints
- turning constraints
- realistic capacity
- planning capacity
- planning occupancy

### Model 2 data

MTO transaction data and the Belt/Box Master provide the process-level data.

The model converts transaction quantities into full boxes and balance quantities, then evaluates Current vs Proposed handling/WIP behaviour.

---

## 10. Current vs Proposed Process Logic

The project distinguishes the physical quantity conversion from the process method.

### Current

A transaction is converted into full boxes plus a balance quantity. The current process can involve loose/shared-bin WIP, subsequent retrieval/rehandling and dispatch.

### Proposed

The same physical demand is not artificially reduced. Full-box quantities remain the same. The proposed method changes how balance quantities and WIP are handled, including the use of dedicated/single-SKU box handling where appropriate.

Therefore:

**Current and Proposed should not differ merely because the proposed model assumes fewer belts.**

The improvement should come from process design, WIP handling, touches, packing/handling time and related operational changes.

---

## 11. Box Conversion Rules

For each transaction:

1. Identify the SKU / belt size.
2. Find its master-table belt-per-box value.
3. Ignore master values of 0 as invalid.
4. If no valid positive standard exists, use the project fallback of **28 belts/box**.
5. Calculate full boxes.
6. Calculate remaining balance quantity.
7. Determine whether a balance box is required under the selected process scenario.

Conceptually:

`Full Boxes = floor(Quantity / Units Per Box)`

`Balance = Quantity − Full Boxes × Units Per Box`

`Total Boxes = Full Boxes + Balance Box Indicator`

The application should retain validation statistics so a manager can see how many rows used actual master standards, fallback values or conflict handling.

---

## 12. Storage Paradox

A key concept of Model 2 is the **storage paradox**.

A process improvement may reduce handling time, manual touches, fatigue and error-risk while simultaneously increasing temporary WIP/storage requirements.

Therefore the project should not report only productivity improvements.

It must show both:

### Process benefit

- processing-time change
- manual-touch change
- fatigue proxy change
- error-risk proxy change

### Storage consequence

- average WIP
- peak WIP
- WIP box count
- peak WIP storage area
- staging utilization
- staging headroom / excess

The manager therefore sees the net operational effect rather than a one-sided improvement claim.

---

## 13. WIP Principle

The project does **not** treat 100% WIP occupancy as a normal target.

The working planning philosophy is approximately **60–70% utilization**, with **65%** as the default planning target.

This is important for practical warehouse management because:

- access must remain available
- demand variability must be absorbed
- unexpected receipts must have somewhere to go
- temporary WIP must not block normal operations
- a theoretically full warehouse is not necessarily an operationally usable warehouse

---

## 14. Managerial Decision Workspace

The application combines Model 1 and Model 2 into a managerial decision view.

The decision logic is intentionally separated into three questions:

### 1. Physical feasibility

Can the warehouse physically and operationally accommodate the required pallet activity?

### 2. Process benefit

Does the proposed process improve time, touches, fatigue proxy or error-risk proxy?

### 3. Storage feasibility

Does the resulting WIP fit inside the approved staging/storage constraint?

A process scenario should not be labelled operationally feasible merely because it improves processing time.

The manager must consider physical capacity, staging capacity, safety, service level, cost and operational validation.

---

## 15. Important Project Metrics

The report may present the following KPI groups.

### Physical KPIs

- Warehouse area (m²)
- Designated pallet-storage area (m²)
- Theoretical capacity
- Realistic capacity
- Planning capacity
- Target occupancy
- Average slot distance
- Peak demand
- Overflow days

### Process KPIs

- Transaction count
- Master standard coverage
- Missing-standard fallback count
- Zero-standard fallback count
- Conflict count
- Current processing time
- Proposed processing time
- Time reduction %
- Current touches
- Proposed touches
- Touch reduction %
- Fatigue proxy
- Error-risk proxy

### Storage KPIs

- Average WIP
- Peak WIP
- Peak WIP storage area
- Staging capacity, when supplied
- Staging utilization
- Staging headroom / excess

---

## 16. Reusability / Cross-Company Design

The system is intended to work beyond the original project/company.

To remain reusable:

- Do not hard-code company-specific capacity.
- Do not hard-code warehouse area.
- Do not hard-code the operating door.
- Do not assume the entire warehouse is pallet storage.
- Do not hard-code the 66/90 result from the original case.
- Keep pallet dimensions configurable.
- Keep aisle and turning requirements configurable.
- Keep planning occupancy configurable in the 60–70% range.
- Use uploaded master data where available.
- Flag missing or invalid master values.
- Keep assumptions visible to the manager.
- Separate measured data from modelled proxies.

The original case values can be used for validation/presentation, but the Digital Twin itself should independently calculate outputs from the uploaded source files.

---

## 17. Known Assumptions and Limitations

### CAD semantic limitation

DXF geometry provides coordinates but does not always identify the business meaning of every line. Layer names and geometry are used as evidence, but user confirmation remains necessary for important operational inputs.

### Capacity limitation

Theoretical capacity is geometric and realistic capacity is a modelled operational estimate. It is not a physical certification of safe warehouse capacity.

### Distance limitation

Straight-line or simplified geometric distances may be used unless a full route network is available. Travel estimates should therefore be treated as model outputs rather than measured forklift telemetry.

### Fatigue and error metrics

Fatigue and error-risk are proxies. They are not clinical measurements or direct worker observations.

### Staging feasibility

If an approved/observed staging-area limit is not supplied, the model should report the required WIP area but should not invent a feasibility threshold.

---

## 18. Application Structure

Current repository structure includes:

```text
SIP-project/
├── app.py
├── core/
│   ├── pipeline.py
│   ├── cad_engine.py
│   ├── layout_engine.py
│   ├── capacity_engine.py
│   ├── simulation_engine.py
│   ├── visualization_engine.py
│   ├── export_engine.py
│   └── mto_actual_engine.py
├── pages/
├── tests/
├── requirements.txt
└── README.md
```

The architecture separates:

- CAD parsing
- layout generation
- capacity calculation
- demand normalization
- simulation
- visualization
- exports
- MTO process analysis

This separation is important for testing and future extension.

---

## 19. Outputs / Deliverables

The project is intended to generate:

1. Warehouse CAD visualization
2. Designated pallet-storage visualization
3. Theoretical vs realistic vs planning capacity table
4. Optimized pallet layout
5. Slot coordinate table
6. Daily occupancy simulation
7. Daily simulation GIF
8. Model 2 transaction-level box-conversion table
9. Current vs Proposed process KPI table
10. WIP simulation
11. Storage Paradox table
12. Physical + Process Decision Workspace
13. Excel exports for analysis/reporting

---

## 20. Original Case Context

The Digital Twin was developed from a warehouse improvement project involving belt storage/handling, MTO/MTA flows and a Poly-V storage area.

Historical project material included a current/proposed storage comparison and a project-specific capacity result. Those historical values should be treated as case-study findings rather than universal model inputs.

The project documentation also distinguishes the physical storage allocation from the total warehouse and supports the need to model a designated storage zone rather than assuming the complete warehouse is available for pallets.

---

## 21. Suggested Report Structure

For the next report-generation chat, use this documentation as the technical project baseline and structure the report around:

1. Executive Summary
2. Background / Business Problem
3. Research Gap / Motivation
4. Objectives
5. Research Questions
6. Literature / Reference Papers
7. Existing Warehouse Process
8. Data Inputs and Data Preparation
9. Digital Twin Architecture
10. Model 1 — Physical Warehouse Twin
11. Model 2 — Process / WIP Twin
12. Storage Paradox
13. Simulation and What-if Analysis
14. Results / Findings
15. Managerial Decision Framework
16. Validation / Assumptions / Limitations
17. Recommendations
18. Conclusion
19. References
20. Appendices

The report should distinguish **observed data**, **derived calculations**, **model assumptions**, **simulation outputs**, and **managerial recommendations**.

---

## 22. Report Generation Notes

When preparing the final academic/SIP report:

- Use the actual project files and outputs as the primary evidence.
- Do not invent measurements that were not supplied.
- Do not present proxy measures as directly observed worker outcomes.
- Clearly identify default values and fallback rules.
- Explain why realistic capacity is lower than theoretical capacity.
- Explain why planning capacity is lower than realistic capacity.
- Explain why a process improvement can increase temporary storage requirements.
- Explain the importance of user-designated pallet-storage areas.
- Present the Digital Twin as a reusable decision-support framework rather than only a JK Fenner-specific calculation.

---

## 23. Current Git / Development Status

The repository is on the `main` branch.

The current application already contains the shared Model 1 / Model 2 architecture and manager decision workspace.

A dedicated pallet-storage-area feature has also been added as a separate Streamlit page for controlled testing before deeper integration into the main workflow.

The latest development work should always be pulled from GitHub before report generation so the report describes the actual implementation rather than an earlier prototype.

---

## 24. One-Paragraph Project Description for the Report

> The Universal Warehouse Digital Twin is a reusable CAD-assisted decision-support framework that integrates warehouse geometry, operational constraints, transaction-level demand and master data to model physical storage capacity and warehouse process behaviour. The framework distinguishes theoretical geometric capacity from realistic operational capacity and from a configurable planning capacity based on a 60–70% occupancy philosophy. It further integrates a transaction-level process twin to evaluate current and proposed handling methods, including box conversion, temporary WIP, processing time, manual touches, fatigue and error-risk proxies. A dedicated pallet-storage-area function allows managers to reserve only a practical portion of the warehouse for pallet storage, making the model suitable for what-if analysis across different warehouse layouts and operating contexts. The resulting physical, process and storage outputs are brought together in a managerial decision workspace so that process improvements can be evaluated against capacity, accessibility and storage consequences rather than on productivity metrics alone.

---

## 25. Handoff Instruction for Next Chat

**Use this file as the master technical/project context for generating the SIP report.**

Before writing numerical results, pull/check the latest repository implementation and use the user's actual uploaded/source files and generated outputs where available. Keep company-specific historical results separate from the reusable Digital Twin logic. Do not silently invent missing values.
