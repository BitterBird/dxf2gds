# dxf2gds — MEMS-aware DXF to GDSII converter

A command-line tool that converts AutoCAD DXF files to GDSII with built-in
manufacturability checks specific to MEMS photolithography.

## Why this exists

AutoCAD is the most common tool for MEMS mask design, but mask shops want GDSII.
The conversion is where most mask errors are introduced: open polylines,
zero-width paths, under-segmented circles, off-grid vertices from AutoCAD's float
precision, and layer-name-to-number mapping mistakes. This tool does the
conversion **and** flags the issues before you send the file to the mask shop.

## Checks performed

| Check | Why it matters |
|---|---|
| Self-intersecting polygons | Mask writers either refuse or produce unpredictable results |
| Zero-width paths | Invisible on the mask, wasted design intent |
| Open polylines | Become paths, not polygons — often rejected by mask shops |
| Acute angles < 20° | Cause notching/footing in DRIE, rounding on laser writer |
| Off-grid vertices | Snap errors on the mask writer, alignment drift |
| Minimum feature size | Below writer resolution → printed feature is wrong |
| Under-segmented circles | Printed "circle" looks like a hexagon |
| Unit mismatch | mm vs µm → 1000× scale error, the #1 MEMS mask mistake |

## Install

```bash
pip install -r requirements.txt
```

## Use

```bash
# Convert and lint
python -m dxf2gds convert input.dxf output.gds --lint-report report.json

# Lint only (on existing GDSII)
python -m dxf2gds lint input.gds

# With explicit layer mapping
python -m dxf2gds convert input.dxf output.gds --layer-map layers.yaml
```

## Roadmap

- [x] DXF parsing (polylines, circles, arcs, lines)
- [x] GDSII output
- [x] 6 MEMS-specific lint checks
- [ ] Streamlit demo UI
- [ ] Pyodide browser version (no upload, mask data stays local)
- [ ] DRIE-specific rules (aspect ratio, open area ratio)

Built by Milan Khambhadiya — MEMS Process Engineer at MechIC GmbH
