"""Command-line interface: `python -m dxf2gds convert ...` and `lint ...`"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
import gdstk
import yaml

from .converter import ConvertOptions, DxfToGdsConverter, convert_file
from .lint import LintOptions, lint_library, summarize


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s  %(message)s",
    )


def _load_layer_map(path: str | None) -> dict[str, tuple[int, int]]:
    if not path:
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    # Accept either {name: number} or {name: [number, datatype]}
    out = {}
    for name, val in data.items():
        if isinstance(val, int):
            out[name] = (val, 0)
        elif isinstance(val, (list, tuple)) and len(val) == 2:
            out[name] = (int(val[0]), int(val[1]))
        else:
            raise ValueError(f"Bad layer-map entry for {name!r}: {val!r}")
    return out


def _load_lint_options(path: str | None) -> LintOptions:
    opt = LintOptions()
    if not path:
        return opt
    with open(path) as f:
        data = yaml.safe_load(f)
    if "min_angle_deg" in data:
        opt.min_angle_deg = float(data["min_angle_deg"])
    if "grid_um" in data:
        opt.grid_um = float(data["grid_um"])
    if "min_feature_size_um" in data:
        opt.min_feature_size_um = {
            int(k): float(v) for k, v in data["min_feature_size_um"].items()
        }
    if "min_spacing_um" in data:
        opt.min_spacing_um = {
            int(k): float(v) for k, v in data["min_spacing_um"].items()
        }
    return opt


@click.group()
@click.option("-v", "--verbose", is_flag=True)
def cli(verbose: bool):
    """DXF to GDSII with MEMS-aware lint checks."""
    _configure_logging(verbose)


@cli.command()
@click.argument("dxf_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("gds_path", type=click.Path(dir_okay=False))
@click.option(
    "--layer-map",
    "layer_map_path",
    type=click.Path(exists=True, dir_okay=False),
    help="YAML file mapping DXF layer names to GDSII layer numbers.",
)
@click.option(
    "--scale",
    type=float,
    default=1000.0,
    show_default=True,
    help="DXF → GDSII scale factor (1000 = mm to µm).",
)
@click.option(
    "--arc-tolerance",
    type=float,
    default=0.01,
    show_default=True,
    help="Max deviation between ideal arc and polygon (µm).",
)
@click.option(
    "--lint/--no-lint",
    default=True,
    help="Run lint checks on the output.",
)
@click.option(
    "--lint-config",
    type=click.Path(exists=True, dir_okay=False),
    help="YAML file with lint options.",
)
@click.option(
    "--lint-report",
    type=click.Path(dir_okay=False),
    help="Write JSON lint report to this path.",
)
def convert(
    dxf_path, gds_path, layer_map_path, scale, arc_tolerance,
    lint, lint_config, lint_report,
):
    """Convert DXF_PATH to GDSII at GDS_PATH."""
    opts = ConvertOptions(
        dxf_to_gds_scale=scale,
        arc_tolerance_um=arc_tolerance,
        layer_map=_load_layer_map(layer_map_path),
    )
    click.echo(f"→ Reading {dxf_path}")
    result = convert_file(dxf_path, gds_path, opts)

    click.echo(
        f"  {result.n_polygons} polygons, {result.n_paths} paths, "
        f"{result.n_skipped} skipped"
    )
    if result.skipped_types:
        click.echo("  Skipped entity types:")
        for t, n in sorted(result.skipped_types.items()):
            click.echo(f"    {t}: {n}")
    click.echo(f"  Layer map used:")
    for name, (num, dt) in sorted(result.layer_map_used.items()):
        click.echo(f"    {name!r} → layer {num}, datatype {dt}")

    click.echo(f"✓ Wrote {gds_path}")

    if lint:
        click.echo("→ Linting output...")
        lint_opts = _load_lint_options(lint_config)
        issues = lint_library(result.library, lint_opts)
        summary = summarize(issues)

        for iss in issues[:20]:  # show first 20 on console
            loc = f"@ ({iss.location[0]:.2f}, {iss.location[1]:.2f})" if iss.location else ""
            click.echo(
                f"  [{iss.severity.value.upper():7}] {iss.check:30} "
                f"L{iss.layer}/D{iss.datatype}  {iss.message} {loc}"
            )
        if len(issues) > 20:
            click.echo(f"  ... and {len(issues) - 20} more")

        click.echo(
            f"  Summary: {summary['error']} errors, "
            f"{summary['warning']} warnings, {summary['info']} info"
        )

        if lint_report:
            with open(lint_report, "w") as f:
                json.dump(
                    {
                        "summary": summary,
                        "issues": [i.to_dict() for i in issues],
                    },
                    f,
                    indent=2,
                )
            click.echo(f"✓ Wrote lint report to {lint_report}")

        if summary["error"] > 0:
            sys.exit(2)


@cli.command()
@click.argument("gds_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--lint-config",
    type=click.Path(exists=True, dir_okay=False),
    help="YAML file with lint options.",
)
@click.option(
    "--report",
    type=click.Path(dir_okay=False),
    help="Write JSON report to this path.",
)
def lint(gds_path, lint_config, report):
    """Lint an existing GDSII file."""
    click.echo(f"→ Reading {gds_path}")
    lib = gdstk.read_gds(gds_path)
    lint_opts = _load_lint_options(lint_config)
    issues = lint_library(lib, lint_opts)
    summary = summarize(issues)

    for iss in issues[:20]:
        loc = f"@ ({iss.location[0]:.2f}, {iss.location[1]:.2f})" if iss.location else ""
        click.echo(
            f"  [{iss.severity.value.upper():7}] {iss.check:30} "
            f"L{iss.layer}/D{iss.datatype}  {iss.message} {loc}"
        )
    if len(issues) > 20:
        click.echo(f"  ... and {len(issues) - 20} more")

    click.echo(
        f"  Summary: {summary['error']} errors, "
        f"{summary['warning']} warnings"
    )

    if report:
        with open(report, "w") as f:
            json.dump(
                {"summary": summary, "issues": [i.to_dict() for i in issues]},
                f,
                indent=2,
            )
        click.echo(f"✓ Wrote report to {report}")

    if summary["error"] > 0:
        sys.exit(2)


if __name__ == "__main__":
    cli()
