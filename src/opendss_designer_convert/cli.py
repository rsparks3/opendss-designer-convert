"""Command line: opendss-designer-convert <format> <files...> -o <outdir>."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from opendss_designer_convert import FORMATS, ConversionError, __version__, convert


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opendss-designer-convert",
        description="Convert a CYME or Synergi Electric model to OpenDSS.",
        epilog="\n".join(f"{f.key}: {f.help}" for f in FORMATS.values()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("format", choices=sorted(FORMATS), help="source format")
    parser.add_argument("files", nargs="+", type=Path, help="source model files")
    parser.add_argument(
        "-o", "--output", required=True, type=Path, help="folder to write the OpenDSS files to"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        # The warnings come back in the result and are printed below.
        logging.getLogger("opendss_designer_convert").propagate = False
    uploads = []
    for path in args.files:
        try:
            uploads.append((path.name, path.read_bytes()))
        except OSError as exc:
            print(f"error: cannot read {path}: {exc.strerror or exc}", file=sys.stderr)
            return 2
    try:
        result = convert(args.format, uploads)
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.output.mkdir(parents=True, exist_ok=True)
    for name, text in result.files:
        (args.output / name).write_text(text, encoding="utf-8")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"Wrote {len(result.files)} files to {args.output}; compile {result.main}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
