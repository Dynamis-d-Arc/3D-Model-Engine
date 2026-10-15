# Riverside Apartment 3B: reference input set (angled walls)

A single-floor apartment of about 121 m² (net) that is deliberately not all straight:

* the **west facade slopes**: 2.00 m east over 10.00 m north (about 11°);
* the **south-east corner is cut at 45°** (2.00 × 2.00 m) in the study;
* the living room has a **curved, glazed bay**: radius 2.13 m, bulging 1.40 m south;
* the bedroom 2 desk follows the sloped wall, and the bay armchairs are turned ±20°.

Everything is generated from one exact design by `make_reference.py`, so the true geometry is known.

| File | What it is | Use it as |
|---|---|---|
| `floor_plan.pdf` | Architectural plan, vector A3 at **1:75**: walls, doors, windows, furniture, dimensions, room names and areas, structural grid 1–5 / A–D | **Brief & files → Floor plan** (upload the PDF, not the PNG: it carries the scale and exact labels) |
| `hvac_plan.pdf` | ACMV layout on the same grid: 3 ducted fan coils, 1 wall split, 7 diffusers, 3 return grilles, ducts with sizes | **Brief & files → HVAC drawing** |
| `floor_plan.png`, `hvac_plan.png` | 150 dpi images of the two sheets | looking at; a PNG plan also works for *Interpret with AI* but has no scale |
| `coordination.png` | Plan + HVAC in blue + furniture numbers in red | checking by eye |
| `furniture_list.csv` | 45 items with catalog id, size, position and rotation | the furniture brief / checking furniture detection |
| `scene_truth.json` | The exact layout as a scene (rooms, walls, openings, furniture) | comparing against what the AI produced |

## In the app

**Short way:** drop `floor_plan.pdf`, `hvac_plan.pdf` and `furniture_list.csv` on the start page together
and press **Make the 3D model** (docs/AUTOPILOT.md). The files are sorted by themselves, the furniture list places the
45 items exactly, and the model is built with the list of what was estimated. Step by step:

1. **New project** "Riverside Apartment 3B", style *Modern*.
2. **Brief**: overall width **14.25 m**, overall depth **10.25 m** (outer faces; the bay projects another 1.40 m),
   wall height **2.70 m**. Room dimensions you know (clear, wall face to face):
   Master Bedroom 4.48 × 2.82, Bathroom 2.28 × 2.82, Ensuite 1.82 × 2.82.
   Description, for example: *Warm modern apartment: oak floors, white walls, a reading nook with two armchairs in the
   curved bay, a home office desk in bedroom 2 along the sloped wall.*
3. Upload `floor_plan.pdf` as the floor plan. It calibrates itself from the printed scale 1:75.
4. Upload `hvac_plan.pdf` as the HVAC drawing.
5. **Interpret with AI**. With gpt-6-astra, expect about 3–10 minutes and roughly $0.75–1.50. Check that the sloped
   wall, the 45° corner and the bay came through (the bay becomes a few straight wall facets).
6. **Review layout → HVAC → Import HVAC drawing** (free, about 20 s).
7. Furniture: **Detect furniture (AI)** reads the drawn furniture from the plan (costs tokens), or use
   `furniture_list.csv` to place or correct items (positions are given both from grid A/1 and in the app's plan frame).
8. Fix what the Problems list shows, save, **Build the 3D model**.

## What the HVAC import should find

Checked with the app's own importer against the exact layout (`scene_truth.json`), no AI involved:

* sheets aligned on all 5 + 4 grid lines, zero offset;
* 4 units: **1FC-01** 500 L/s (hall ceiling), **1FC-02** 200 L/s (master bedroom), **1FC-03** 150 L/s (bedroom 2),
  **AC-01** 100 L/s wall split (study);
* 7 supply diffusers, every airflow read from its label and linked to the right unit
  (1FC-01: kitchen 100 + dining 125 + bay 125 + living 150 = 500 L/s);
* 3 return grilles; 7 duct runs, about 17 m (SAD 400X250, 300X200, 250X200).

With a layout from *Interpret with AI* the rooms can differ a little, so a unit or diffuser near a wall may land in
the neighbouring room; the import report lists anything to check.

## Regenerating

```bash
.venv/Scripts/python.exe samples/angled_apartment/make_reference.py
```

The HVAC sheet follows what `twin.hvac` reads: a grey architectural underlay, the dash-dot grid drawn as separate
dashes, single-stroke lettering (the importer reads labels with OCR from the line work), 4-way diffusers (plain
nested squares are read by the OCR as the character 回), grilles with one diagonal, fan coils with two marker boxes,
and ducts as separate side lines with their size printed clear of them.
