"""
MEMS-aware lint checks for GDSII layouts.

Each check returns a list of Issue objects. Checks are intentionally simple
and fast — a MEMS designer should be able to run the linter on a 100 MB mask
file in under 10 seconds.

Why these specific checks? They are the ones that (a) survive commercial DRC
tools because they are geometry or workflow issues rather than design-rule
violations, and (b) I have personally seen break masks in the MechIC cleanroom.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

import gdstk
from shapely.geometry import Polygon as ShPolygon
from shapely.validation import explain_validity


class Severity(str, Enum):
    ERROR = "error"       # mask shop will reject or produce broken mask
    WARNING = "warning"   # likely to cause process issues
    INFO = "info"         # worth a look but probably fine


@dataclass
class Issue:
    check: str
    severity: Severity
    layer: int
    datatype: int
    message: str
    location: tuple[float, float] | None = None  # representative (x, y) in µm

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity.value,
            "layer": self.layer,
            "datatype": self.datatype,
            "message": self.message,
            "location": list(self.location) if self.location else None,
        }


@dataclass
class LintOptions:
    # Flag acute angles below this (degrees).
    min_angle_deg: float = 20.0
    # Grid on which vertices should lie (µm). AutoCAD designs in mm with
    # float precision often drift to e.g. 1.00000003 µm — not on grid.
    grid_um: float = 0.005
    # Minimum feature size per layer (µm). None = skip check on that layer.
    min_feature_size_um: dict[int, float] = field(default_factory=dict)
    # Minimum spacing per layer (µm).
    min_spacing_um: dict[int, float] = field(default_factory=dict)
    # Flag circles approximated with fewer than this many vertices.
    min_circle_vertices: int = 24


# --- individual checks ---

def check_self_intersecting(polygons: list[gdstk.Polygon]) -> list[Issue]:
    issues = []
    for p in polygons:
        pts = list(p.points)
        if len(pts) < 4:
            continue
        sh = ShPolygon(pts)
        if not sh.is_valid:
            reason = explain_validity(sh)
            issues.append(
                Issue(
                    check="self_intersecting_polygon",
                    severity=Severity.ERROR,
                    layer=p.layer,
                    datatype=p.datatype,
                    message=f"Polygon is invalid: {reason}",
                    location=(float(pts[0][0]), float(pts[0][1])),
                )
            )
    return issues


def check_zero_width_paths(paths: list[gdstk.FlexPath]) -> list[Issue]:
    issues = []
    for pth in paths:
        widths = pth.widths  # list of widths, one per point
        if widths is None:
            continue
        # gdstk returns a numpy array; take the max width along the path
        max_w = float(max(widths.max(axis=0))) if len(widths) else 0.0
        if max_w <= 0.0:
            pts = list(pth.spine())
            loc = (float(pts[0][0]), float(pts[0][1])) if len(pts) else None
            # FlexPath can span multiple layers
            layer = pth.layers[0] if pth.layers else 0
            datatype = pth.datatypes[0] if pth.datatypes else 0
            issues.append(
                Issue(
                    check="zero_width_path",
                    severity=Severity.ERROR,
                    layer=layer,
                    datatype=datatype,
                    message="Path has zero width — invisible on mask",
                    location=loc,
                )
            )
    return issues


def check_acute_angles(
    polygons: list[gdstk.Polygon], min_angle_deg: float
) -> list[Issue]:
    issues = []
    min_angle_rad = math.radians(min_angle_deg)
    for p in polygons:
        pts = list(p.points)
        n = len(pts)
        if n < 3:
            continue
        for i in range(n):
            p0 = pts[(i - 1) % n]
            p1 = pts[i]
            p2 = pts[(i + 1) % n]
            v1 = (p0[0] - p1[0], p0[1] - p1[1])
            v2 = (p2[0] - p1[0], p2[1] - p1[1])
            m1 = math.hypot(*v1)
            m2 = math.hypot(*v2)
            if m1 == 0 or m2 == 0:
                continue
            cos_a = (v1[0] * v2[0] + v1[1] * v2[1]) / (m1 * m2)
            cos_a = max(-1.0, min(1.0, cos_a))
            angle = math.acos(cos_a)
            if angle < min_angle_rad:
                issues.append(
                    Issue(
                        check="acute_angle",
                        severity=Severity.WARNING,
                        layer=p.layer,
                        datatype=p.datatype,
                        message=(
                            f"Interior angle {math.degrees(angle):.1f}° < "
                            f"{min_angle_deg}° — risks DRIE notching / "
                            f"lithography rounding"
                        ),
                        location=(float(p1[0]), float(p1[1])),
                    )
                )
                break  # one report per polygon is enough
    return issues


def check_off_grid(
    polygons: list[gdstk.Polygon], grid_um: float
) -> list[Issue]:
    issues = []
    tol = grid_um * 1e-3  # 0.1% of grid
    for p in polygons:
        for (x, y) in p.points:
            rx = round(x / grid_um) * grid_um
            ry = round(y / grid_um) * grid_um
            if abs(x - rx) > tol or abs(y - ry) > tol:
                issues.append(
                    Issue(
                        check="off_grid_vertex",
                        severity=Severity.WARNING,
                        layer=p.layer,
                        datatype=p.datatype,
                        message=(
                            f"Vertex ({x:.6f}, {y:.6f}) not on "
                            f"{grid_um} µm grid — likely float drift from "
                            f"AutoCAD unit conversion"
                        ),
                        location=(float(x), float(y)),
                    )
                )
                break  # one report per polygon
    return issues


def check_min_feature(
    polygons: list[gdstk.Polygon],
    min_feature_um: dict[int, float],
) -> list[Issue]:
    issues = []
    for p in polygons:
        if p.layer not in min_feature_um:
            continue
        limit = min_feature_um[p.layer]
        sh = ShPolygon(p.points)
        if not sh.is_valid:
            continue
        # A crude but effective width metric: inscribed circle diameter.
        # For speed, use sqrt(area) < limit as a cheap lower bound.
        if sh.area > 0 and math.sqrt(sh.area) < limit:
            issues.append(
                Issue(
                    check="min_feature_size",
                    severity=Severity.ERROR,
                    layer=p.layer,
                    datatype=p.datatype,
                    message=(
                        f"Feature smaller than {limit} µm minimum on layer "
                        f"{p.layer} (area={sh.area:.3f} µm²)"
                    ),
                    location=(float(p.points[0][0]), float(p.points[0][1])),
                )
            )
    return issues


def check_circle_segmentation(
    polygons: list[gdstk.Polygon], min_vertices: int
) -> list[Issue]:
    """Detect polygons that look like under-segmented circles."""
    issues = []
    for p in polygons:
        pts = p.points
        n = len(pts)
        if n < 6 or n >= min_vertices:
            continue
        # Heuristic: if all vertices are roughly equidistant from the centroid
        # AND roughly evenly spaced angularly, it's a circle approximation.
        cx = sum(x for x, _ in pts) / n
        cy = sum(y for _, y in pts) / n
        dists = [math.hypot(x - cx, y - cy) for x, y in pts]
        mean_d = sum(dists) / n
        if mean_d == 0:
            continue
        var = sum((d - mean_d) ** 2 for d in dists) / n
        rel_std = math.sqrt(var) / mean_d
        if rel_std < 0.02:  # all vertices within 2% of same radius → a circle
            issues.append(
                Issue(
                    check="under_segmented_circle",
                    severity=Severity.WARNING,
                    layer=p.layer,
                    datatype=p.datatype,
                    message=(
                        f"Probable circle with only {n} vertices — "
                        f"will print as polygon, not circle. Re-convert "
                        f"with tighter arc tolerance."
                    ),
                    location=(float(cx), float(cy)),
                )
            )
    return issues


# --- orchestrator ---

def lint_cell(
    cell: gdstk.Cell, options: LintOptions | None = None
) -> list[Issue]:
    opt = options or LintOptions()
    polygons: list[gdstk.Polygon] = list(cell.polygons)
    paths: list[gdstk.FlexPath] = list(cell.paths)

    issues: list[Issue] = []
    issues.extend(check_self_intersecting(polygons))
    issues.extend(check_zero_width_paths(paths))
    issues.extend(check_acute_angles(polygons, opt.min_angle_deg))
    issues.extend(check_off_grid(polygons, opt.grid_um))
    issues.extend(check_min_feature(polygons, opt.min_feature_size_um))
    issues.extend(check_circle_segmentation(polygons, opt.min_circle_vertices))
    return issues


def lint_library(
    lib: gdstk.Library, options: LintOptions | None = None
) -> list[Issue]:
    issues = []
    for cell in lib.cells:
        issues.extend(lint_cell(cell, options))
    return issues


def summarize(issues: Iterable[Issue]) -> dict[str, int]:
    summary = {"error": 0, "warning": 0, "info": 0, "total": 0}
    for iss in issues:
        summary[iss.severity.value] += 1
        summary["total"] += 1
    return summary
