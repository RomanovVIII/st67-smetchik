#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from artifact_writer import CorrectionError, create_corrected_artifacts
from smetchik_engine import InspectionInputError, inspect_input


MAX_CLI_JSON_BYTES = 2_000_000
MAX_CONTEXT_JSON_BYTES = 1024 * 1024


class CliError(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smetchik")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("input", type=Path)
    inspect_parser.add_argument("--mode", choices=("light", "full"), required=True)
    inspect_parser.add_argument("--purpose")
    inspect_parser.add_argument("--context-json", type=Path)
    inspect_parser.add_argument("--object-name")
    inspect_parser.add_argument("--work-type")
    inspect_parser.add_argument("--funding-source")
    inspect_parser.add_argument("--region-or-price-zone")
    inspect_parser.add_argument("--price-level-date")
    inspect_parser.add_argument("--calculation-method")
    inspect_parser.add_argument("--stage")
    inspect_parser.add_argument(
        "--document-set",
        action="append",
        help="Элемент состава документов; флаг можно повторять.",
    )
    inspect_parser.add_argument("--output-json", type=Path)
    correct_parser = subparsers.add_parser("correct")
    correct_parser.add_argument("source", type=Path)
    correct_parser.add_argument("--corrections-json", type=Path, required=True)
    correct_parser.add_argument("--output", type=Path, required=True)
    correct_parser.add_argument("--changelog", type=Path, required=True)
    return parser


def _read_context(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    descriptor: int | None = None
    try:
        initial = path.lstat()
        if stat.S_ISLNK(initial.st_mode):
            raise CliError("context-json symlink is forbidden")
        if not stat.S_ISREG(initial.st_mode):
            raise CliError("context-json must be a regular file")
        if initial.st_size > MAX_CONTEXT_JSON_BYTES:
            raise CliError("context-json size exceeds the 1 MiB limit")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != initial.st_dev
            or opened.st_ino != initial.st_ino
        ):
            raise CliError("context-json changed during safe preflight")
        if opened.st_size > MAX_CONTEXT_JSON_BYTES:
            raise CliError("context-json size exceeds the 1 MiB limit")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = None
            payload = stream.read(MAX_CONTEXT_JSON_BYTES + 1)
        if len(payload) > MAX_CONTEXT_JSON_BYTES:
            raise CliError("context-json size exceeds the 1 MiB limit")
        value = json.loads(payload.decode("utf-8"))
    except CliError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CliError("context-json is unreadable or invalid JSON") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise CliError("context-json must contain a JSON object")
    return value


def _unlink_owned(path: Path, metadata: os.stat_result | None) -> None:
    if metadata is None:
        return
    try:
        current = path.lstat()
        if (
            stat.S_ISREG(current.st_mode)
            and current.st_dev == metadata.st_dev
            and current.st_ino == metadata.st_ino
        ):
            path.unlink()
    except FileNotFoundError:
        pass


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_create(output: Path, payload: bytes) -> None:
    parent = output.parent
    descriptor: int | None = None
    temporary: Path | None = None
    metadata: os.stat_result | None = None
    published = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=parent,
        )
        temporary = Path(temporary_name)
        metadata = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output, follow_symlinks=False)
        published = True
        temporary.unlink()
        temporary = None
        _fsync_directory(parent)
    except FileExistsError as error:
        if published:
            _unlink_owned(output, metadata)
        if temporary is not None:
            _unlink_owned(temporary, metadata)
        raise CliError("output-json already exists; overwrite is forbidden") from error
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        if published:
            _unlink_owned(output, metadata)
        if temporary is not None:
            _unlink_owned(temporary, metadata)
        try:
            _fsync_directory(parent)
        except OSError:
            pass
        raise CliError("output-json could not be created safely") from error


def _write_result(result: dict[str, Any], output: Path | None) -> None:
    try:
        serialized = json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise CliError("result is not finite valid JSON") from error
    payload = (serialized + "\n").encode("utf-8")
    if len(payload) > MAX_CLI_JSON_BYTES:
        raise CliError(
            "result JSON size limit exceeded; split the package or retry with a smaller scope"
        )
    if output is None:
        sys.stdout.write(serialized + "\n")
        return
    _atomic_create(output, payload)


def _merged_context(args: argparse.Namespace) -> dict[str, Any]:
    context = _read_context(args.context_json)
    explicit = {
        "object": args.object_name,
        "work_type": args.work_type,
        "funding_source": args.funding_source,
        "region_or_price_zone": args.region_or_price_zone,
        "price_level_date": args.price_level_date,
        "calculation_method": args.calculation_method,
        "stage": args.stage,
        "document_set": args.document_set,
    }
    for field, value in explicit.items():
        if value is not None:
            context[field] = value
    return context


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            source = args.input
            if not source.exists() or not (source.is_file() or source.is_dir()):
                raise CliError("input does not exist or is not a regular file/directory")
            result = inspect_input(
                source,
                mode=args.mode,
                purpose=args.purpose,
                context=_merged_context(args),
            )
            _write_result(result, args.output_json)
        else:
            result = create_corrected_artifacts(
                args.source,
                args.corrections_json,
                args.output,
                args.changelog,
            )
            _write_result(result, None)
        return 0
    except (CliError, CorrectionError, InspectionInputError) as error:
        sys.stderr.write(f"smetchik: {error}\n")
        return 2
    except Exception:
        sys.stderr.write("smetchik: inspection failed safely\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
