"""
DXF to GDSII conversion.

The hard part is not reading DXF or writing GDSII — both libraries make that
easy. The hard part is handling the edge cases that break mask shops:
- AutoCAD circles must be segmented into polygons (GDSII has no circles)
- Open polylines must become paths, closed ones must become polygons
- Arc resolution must be chosen to balance fidelity vs. file size
- Layer names ('Metal1', 'Poly') must map to integer layer numbers for GDSII
- Units: AutoCAD is usually mm, MEMS GDSII is almost always µm
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
import gdstk
from ezdxf.document import Drawing

log = logging.getLogger(__name__)


@dataclass
class ConvertOptions:
    """Conversion options. Defaults are tuned for typical MEMS workflow."""

    # DXF is usually mm, GDSII is µm. 1 mm = 1000 µm.
    dxf_to_gds_scale: float = 1000.0

    # GDSII stores coordinates as integer multiples of `precision` (in metres).
    # unit=1e-6 means 1 GDSII user-unit = 1 µm.
    # precision=1e-9 means internal resolution = 1 nm. Standard for MEMS.
    gds_unit: float = 1e-6
    gds_precision: float = 1e-9

    # How finely to segment circles and arcs. Tolerance is the maximum
    # deviation between the ideal curve and the polygon, in µm.
    arc_tolerance_um: float = 0.01

    # Maximum number of vertices per GDSII polygon (GDSII spec limit is 8191
    # for some tools, 199 for very old ones). 8000 is a safe modern default.
    max_vertices: int = 8000

    # If True, unknown/unmappable entities are skipped with a warning.
    # If False, conversion fails. For production use True with a good log.
    skip_unknown: bool = True

    # Layer name -> (layer number, datatype). If a layer is not in this map,
    # it is auto-assigned the next free integer starting from 0.
    layer_map: dict[str, tuple[int, int]] = field(default_factory=dict)

    # Name of the top GDSII cell.
    top_cell_name: str = "TOP"


@dataclass
class ConvertResult:
    """What the converter produced and what it skipped."""

    library: gdstk.Library
    top_cell: gdstk.Cell
    n_polygons: int = 0
    n_paths: int = 0
    n_skipped: int = 0
    skipped_types: dict[str, int] = field(default_factory=dict)
    layer_map_used: dict[str, tuple[int, int]] = field(default_factory=dict)


class DxfToGdsConverter:
    """Convert an ezdxf Drawing into a gdstk Library."""

    def __init__(self, options: ConvertOptions | None = None):
        self.opt = options or ConvertOptions()
        self._next_layer_num = 0
        self._layer_map: dict[str, tuple[int, int]] = dict(self.opt.layer_map)

    # ---- public ----

    def convert(self, doc: Drawing) -> ConvertResult:
        lib = gdstk.Library(
            name="LIB",
            unit=self.opt.gds_unit,
            precision=self.opt.gds_precision,
        )
        top = lib.new_cell(self.opt.top_cell_name)
        result = ConvertResult(library=lib, top_cell=top)

        msp = doc.modelspace()
        for entity in msp:
            try:
                self._convert_entity(entity, top, result)
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to convert %s: %s", entity.dxftype(), exc)
                result.n_skipped += 1
                result.skipped_types[entity.dxftype()] = (
                    result.skipped_types.get(entity.dxftype(), 0) + 1
                )

        result.layer_map_used = dict(self._layer_map)
        return result

    # ---- internals ----

    def _layer_for(self, layer_name: str) -> tuple[int, int]:
        """Return (layer_number, datatype) for a DXF layer name."""
        if layer_name in self._layer_map:
            return self._layer_map[layer_name]
        # If the layer name is literally a number, use it.
        if layer_name.isdigit():
            num = int(layer_name)
        else:
            num = self._next_layer_num
            while any(v[0] == num for v in self._layer_map.values()):
                num += 1
            self._next_layer_num = num + 1
        self._layer_map[layer_name] = (num, 0)
        return self._layer_map[layer_name]

    def _scale(self, p: tuple[float, float]) -> tuple[float, float]:
        s = self.opt.dxf_to_gds_scale
        return (p[0] * s, p[1] * s)

    def _convert_entity(
        self, entity, cell: gdstk.Cell, result: ConvertResult
    ) -> None:
        dxftype = entity.dxftype()
        layer_name = getattr(entity.dxf, "layer", "0")
        layer, datatype = self._layer_for(layer_name)

        if dxftype == "LWPOLYLINE":
            self._handle_lwpolyline(entity, cell, layer, datatype, result)
        elif dxftype == "POLYLINE":
            self._handle_polyline(entity, cell, layer, datatype, result)
        elif dxftype == "CIRCLE":
            self._handle_circle(entity, cell, layer, datatype, result)
        elif dxftype == "ARC":
            self._handle_arc(entity, cell, layer, datatype, result)
        elif dxftype == "LINE":
            self._handle_line(entity, cell, layer, datatype, result)
        elif dxftype in {"INSERT", "TEXT", "MTEXT", "HATCH", "DIMENSION"}:
            # These need pre-processing in AutoCAD (explode, convert text to
            # polygons, flatten hatches). We refuse rather than silently drop.
            if self.opt.skip_unknown:
                log.info(
                    "Skipping %s on layer %r — run EXPLODE/TXTEXP in AutoCAD first",
                    dxftype,
                    layer_name,
                )
                result.n_skipped += 1
                result.skipped_types[dxftype] = (
                    result.skipped_types.get(dxftype, 0) + 1
                )
            else:
                raise ValueError(
                    f"{dxftype} not supported; explode/flatten in AutoCAD first"
                )
        else:
            if self.opt.skip_unknown:
                result.n_skipped += 1
                result.skipped_types[dxftype] = (
                    result.skipped_types.get(dxftype, 0) + 1
                )
            else:
                raise ValueError(f"Unsupported DXF entity: {dxftype}")

    # --- entity handlers ---

    def _handle_lwpolyline(self, e, cell, layer, datatype, result):
        points = [self._scale((p[0], p[1])) for p in e.get_points("xy")]
        if len(points) < 2:
            return
        closed = bool(e.closed)
        if closed and len(points) >= 3:
            cell.add(gdstk.Polygon(points, layer=layer, datatype=datatype))
            result.n_polygons += 1
        else:
            # Open polyline → GDSII path. Width from DXF if present, else 0.
            width = 0.0
            # ezdxf stores const width in dxf.const_width
            const_w = getattr(e.dxf, "const_width", 0.0) or 0.0
            width = float(const_w) * self.opt.dxf_to_gds_scale
            cell.add(
                gdstk.FlexPath(
                    points, width=width, layer=layer, datatype=datatype
                )
            )
            result.n_paths += 1

    def _handle_polyline(self, e, cell, layer, datatype, result):
        points = [self._scale((v.dxf.location.x, v.dxf.location.y)) for v in e.vertices]
        if len(points) < 2:
            return
        if e.is_closed and len(points) >= 3:
            cell.add(gdstk.Polygon(points, layer=layer, datatype=datatype))
            result.n_polygons += 1
        else:
            cell.add(
                gdstk.FlexPath(points, width=0.0, layer=layer, datatype=datatype)
            )
            result.n_paths += 1

    def _handle_circle(self, e, cell, layer, datatype, result):
        cx, cy = self._scale((e.dxf.center.x, e.dxf.center.y))
        r = e.dxf.radius * self.opt.dxf_to_gds_scale
        # gdstk.ellipse handles the tolerance-based segmentation for us.
        poly = gdstk.ellipse(
            (cx, cy),
            r,
            tolerance=self.opt.arc_tolerance_um,
            layer=layer,
            datatype=datatype,
        )
        cell.add(poly)
        result.n_polygons += 1

    def _handle_arc(self, e, cell, layer, datatype, result):
        # Arcs have no area. Convert to a path with zero width. The lint
        # stage will flag zero-width paths — that's correct behaviour because
        # arcs on a mask usually mean the user forgot to close them.
        cx, cy = self._scale((e.dxf.center.x, e.dxf.center.y))
        r = e.dxf.radius * self.opt.dxf_to_gds_scale
        a0 = math.radians(e.dxf.start_angle)
        a1 = math.radians(e.dxf.end_angle)
        if a1 < a0:
            a1 += 2 * math.pi
        # Segment count based on tolerance
        n = max(8, int(abs(a1 - a0) / math.sqrt(2 * self.opt.arc_tolerance_um / r))
                if r > 0 else 16)
        angles = [a0 + (a1 - a0) * i / n for i in range(n + 1)]
        points = [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles]
        cell.add(
            gdstk.FlexPath(points, width=0.0, layer=layer, datatype=datatype)
        )
        result.n_paths += 1

    def _handle_line(self, e, cell, layer, datatype, result):
        p0 = self._scale((e.dxf.start.x, e.dxf.start.y))
        p1 = self._scale((e.dxf.end.x, e.dxf.end.y))
        cell.add(
            gdstk.FlexPath([p0, p1], width=0.0, layer=layer, datatype=datatype)
        )
        result.n_paths += 1


def convert_file(
    dxf_path: str | Path,
    gds_path: str | Path,
    options: ConvertOptions | None = None,
) -> ConvertResult:
    """Convenience: read a DXF, write a GDSII, return the result."""
    doc = ezdxf.readfile(str(dxf_path))
    conv = DxfToGdsConverter(options)
    result = conv.convert(doc)
    result.library.write_gds(str(gds_path))
    return result
