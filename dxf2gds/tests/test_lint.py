"""
Smoke tests: build a GDSII in memory with known-bad geometry and verify each
lint check fires. This is the fastest way to develop and refactor the linter.

Run: pytest tests/ -v
"""

import math
import gdstk

from dxf2gds.lint import (
    LintOptions,
    check_acute_angles,
    check_circle_segmentation,
    check_off_grid,
    check_self_intersecting,
    check_zero_width_paths,
    lint_library,
)


def _make_library():
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    cell = lib.new_cell("TOP")
    return lib, cell


def test_self_intersecting_bowtie():
    lib, cell = _make_library()
    # A bow-tie: self-intersecting quadrilateral.
    cell.add(gdstk.Polygon([(0, 0), (10, 10), (10, 0), (0, 10)], layer=1))
    issues = check_self_intersecting(list(cell.polygons))
    assert len(issues) == 1
    assert issues[0].check == "self_intersecting_polygon"


def test_zero_width_path_detected():
    lib, cell = _make_library()
    cell.add(gdstk.FlexPath([(0, 0), (10, 0)], width=0.0, layer=2))
    issues = check_zero_width_paths(list(cell.paths))
    assert len(issues) == 1


def test_path_with_width_passes():
    lib, cell = _make_library()
    cell.add(gdstk.FlexPath([(0, 0), (10, 0)], width=1.0, layer=2))
    issues = check_zero_width_paths(list(cell.paths))
    assert issues == []


def test_acute_angle_triangle():
    lib, cell = _make_library()
    # A very thin triangle — one angle ~5°.
    cell.add(gdstk.Polygon([(0, 0), (100, 0), (100, 8)], layer=3))
    issues = check_acute_angles(list(cell.polygons), min_angle_deg=20.0)
    assert len(issues) == 1


def test_right_angle_polygon_passes():
    lib, cell = _make_library()
    cell.add(gdstk.Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], layer=3))
    issues = check_acute_angles(list(cell.polygons), min_angle_deg=20.0)
    assert issues == []


def test_off_grid_vertex():
    lib, cell = _make_library()
    # 1.00000003 µm — typical float-drift from mm-to-µm conversion.
    cell.add(gdstk.Polygon([(0, 0), (1.00000003, 0), (1, 1)], layer=4))
    issues = check_off_grid(list(cell.polygons), grid_um=0.005)
    assert len(issues) == 1


def test_on_grid_passes():
    lib, cell = _make_library()
    cell.add(gdstk.Polygon([(0, 0), (1.0, 0), (1.0, 1.0)], layer=4))
    issues = check_off_grid(list(cell.polygons), grid_um=0.005)
    assert issues == []


def test_under_segmented_circle():
    lib, cell = _make_library()
    # 8-sided "circle".
    r = 10.0
    pts = [
        (r * math.cos(2 * math.pi * i / 8), r * math.sin(2 * math.pi * i / 8))
        for i in range(8)
    ]
    cell.add(gdstk.Polygon(pts, layer=5))
    issues = check_circle_segmentation(list(cell.polygons), min_vertices=24)
    assert len(issues) == 1
    assert "8 vertices" in issues[0].message


def test_square_not_flagged_as_circle():
    lib, cell = _make_library()
    cell.add(gdstk.Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], layer=5))
    issues = check_circle_segmentation(list(cell.polygons), min_vertices=24)
    assert issues == []


def test_full_lint_run():
    """End-to-end: a library with multiple issues should report them all."""
    lib, cell = _make_library()
    # Self-intersecting
    cell.add(gdstk.Polygon([(0, 0), (10, 10), (10, 0), (0, 10)], layer=1))
    # Acute angle
    cell.add(gdstk.Polygon([(100, 0), (200, 0), (200, 8)], layer=1))
    # Zero-width path
    cell.add(gdstk.FlexPath([(0, 50), (10, 50)], width=0.0, layer=2))
    # Under-segmented circle
    r = 5.0
    pts = [
        (300 + r * math.cos(2 * math.pi * i / 6),
         r * math.sin(2 * math.pi * i / 6))
        for i in range(6)
    ]
    cell.add(gdstk.Polygon(pts, layer=3))

    issues = lint_library(lib, LintOptions(min_angle_deg=20.0))
    checks_seen = {i.check for i in issues}
    assert "self_intersecting_polygon" in checks_seen
    assert "acute_angle" in checks_seen
    assert "zero_width_path" in checks_seen
    assert "under_segmented_circle" in checks_seen
