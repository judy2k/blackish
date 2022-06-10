#!/usr/bin/env python3

import asyncio
import inspect
import io
import logging
import multiprocessing
import os
import sys
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from platform import system
from tempfile import TemporaryDirectory
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    TypeVar,
    Union,
)
from unittest.mock import MagicMock, patch

import click
import pytest
import re
from click import unstyle
from click.testing import CliRunner
from pathspec import PathSpec

import grey
import grey.files
from grey import Feature, TargetVersion
from grey import re_compile_maybe_verbose as compile_pattern
from grey.cache import get_cache_dir, get_cache_file
from grey.debug import DebugVisitor
from grey.output import color_diff, diff
from grey.report import Report

# Import other test classes
from tests.util import (
    DATA_DIR,
    DEFAULT_MODE,
    DETERMINISTIC_HEADER,
    PROJECT_ROOT,
    PY36_VERSIONS,
    THIS_DIR,
    BlackBaseTestCase,
    assert_format,
    change_directory,
    dump_to_stderr,
    ff,
    fs,
    read_data,
    get_case_path,
    read_data_from_file,
)

THIS_FILE = Path(__file__)
EMPTY_CONFIG = THIS_DIR / "data" / "empty_pyproject.toml"
PY36_ARGS = [f"--target-version={version.name.lower()}" for version in PY36_VERSIONS]
DEFAULT_EXCLUDE = grey.re_compile_maybe_verbose(grey.const.DEFAULT_EXCLUDES)
DEFAULT_INCLUDE = grey.re_compile_maybe_verbose(grey.const.DEFAULT_INCLUDES)
T = TypeVar("T")
R = TypeVar("R")

# Match the time output in a diff, but nothing else
DIFF_TIME = re.compile(r"\t[\d\-:+\. ]+")


@contextmanager
def cache_dir(exists: bool = True) -> Iterator[Path]:
    with TemporaryDirectory() as workspace:
        cache_dir = Path(workspace)
        if not exists:
            cache_dir = cache_dir / "new"
        with patch("grey.cache.CACHE_DIR", cache_dir):
            yield cache_dir


@contextmanager
def event_loop() -> Iterator[None]:
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield

    finally:
        loop.close()


class FakeContext(click.Context):
    """A fake click Context for when calling functions that need it."""

    def __init__(self) -> None:
        self.default_map: Dict[str, Any] = {}
        # Dummy root, since most of the tests don't care about it
        self.obj: Dict[str, Any] = {"root": PROJECT_ROOT}


class FakeParameter(click.Parameter):
    """A fake click Parameter for when calling functions that need it."""

    def __init__(self) -> None:
        pass


class BlackRunner(CliRunner):
    """Make sure STDOUT and STDERR are kept separate when testing Black via its CLI."""

    def __init__(self) -> None:
        super().__init__(mix_stderr=False)


def invokeBlack(
    args: List[str], exit_code: int = 0, ignore_config: bool = True
) -> None:
    runner = BlackRunner()
    if ignore_config:
        args = ["--verbose", "--config", str(THIS_DIR / "empty.toml"), *args]
    result = runner.invoke(grey.main, args, catch_exceptions=False)
    assert result.stdout_bytes is not None
    assert result.stderr_bytes is not None
    msg = (
        f"Failed with args: {args}\n"
        f"stdout: {result.stdout_bytes.decode()!r}\n"
        f"stderr: {result.stderr_bytes.decode()!r}\n"
        f"exception: {result.exception}"
    )
    assert result.exit_code == exit_code, msg


class BlackTestCase(BlackBaseTestCase):
    invokeBlack = staticmethod(invokeBlack)

    def test_empty_ff(self) -> None:
        expected = ""
        tmp_file = Path(grey.dump_to_file())
        try:
            self.assertFalse(ff(tmp_file, write_back=grey.WriteBack.YES))
            with open(tmp_file, encoding="utf8") as f:
                actual = f.read()
        finally:
            os.unlink(tmp_file)
        self.assertFormatEqual(expected, actual)

    def test_experimental_string_processing_warns(self) -> None:
        self.assertWarns(
            grey.mode.Deprecated, grey.Mode, experimental_string_processing=True
        )

    def test_piping(self) -> None:
        source, expected = read_data_from_file(
            PROJECT_ROOT / "src/grey/__init__.py"
        )
        result = BlackRunner().invoke(
            grey.main,
            [
                "-",
                "--fast",
                f"--line-length={grey.DEFAULT_LINE_LENGTH}",
                f"--config={EMPTY_CONFIG}",
            ],
            input=BytesIO(source.encode("utf8")),
        )
        self.assertEqual(result.exit_code, 0)
        self.assertFormatEqual(expected, result.output)
        if source != result.output:
            grey.assert_equivalent(source, result.output)
            grey.assert_stable(source, result.output, DEFAULT_MODE)

    def test_piping_diff(self) -> None:
        diff_header = re.compile(
            r"(STDIN|STDOUT)\t\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d\.\d\d\d\d\d\d "
            r"\+\d\d\d\d"
        )
        source, _ = read_data("simple_cases", "expression.py")
        expected, _ = read_data("simple_cases", "expression.diff")
        args = [
            "-",
            "--fast",
            f"--line-length={grey.DEFAULT_LINE_LENGTH}",
            "--diff",
            f"--config={EMPTY_CONFIG}",
        ]
        result = BlackRunner().invoke(
            grey.main, args, input=BytesIO(source.encode("utf8"))
        )
        self.assertEqual(result.exit_code, 0)
        actual = diff_header.sub(DETERMINISTIC_HEADER, result.output)
        actual = actual.rstrip() + "\n"  # the diff output has a trailing space
        self.assertEqual(expected, actual)

    def test_piping_diff_with_color(self) -> None:
        source, _ = read_data("simple_cases", "expression.py")
        args = [
            "-",
            "--fast",
            f"--line-length={grey.DEFAULT_LINE_LENGTH}",
            "--diff",
            "--color",
            f"--config={EMPTY_CONFIG}",
        ]
        result = BlackRunner().invoke(
            grey.main, args, input=BytesIO(source.encode("utf8"))
        )
        actual = result.output
        # Again, the contents are checked in a different test, so only look for colors.
        self.assertIn("\033[1m", actual)
        self.assertIn("\033[36m", actual)
        self.assertIn("\033[32m", actual)
        self.assertIn("\033[31m", actual)
        self.assertIn("\033[0m", actual)

    @patch("grey.dump_to_file", dump_to_stderr)
    def _test_wip(self) -> None:
        source, expected = read_data("miscellaneous", "wip")
        sys.settrace(tracefunc)
        mode = replace(
            DEFAULT_MODE,
            experimental_string_processing=False,
            target_versions={grey.TargetVersion.PY38},
        )
        actual = fs(source, mode=mode)
        sys.settrace(None)
        self.assertFormatEqual(expected, actual)
        grey.assert_equivalent(source, actual)
        grey.assert_stable(source, actual, grey.FileMode())

    def test_pep_572_version_detection(self) -> None:
        source, _ = read_data("py_38", "pep_572")
        root = grey.lib2to3_parse(source)
        features = grey.get_features_used(root)
        self.assertIn(grey.Feature.ASSIGNMENT_EXPRESSIONS, features)
        versions = grey.detect_target_versions(root)
        self.assertIn(grey.TargetVersion.PY38, versions)

    def test_expression_ff(self) -> None:
        source, expected = read_data("simple_cases", "expression.py")
        tmp_file = Path(grey.dump_to_file(source))
        try:
            self.assertTrue(ff(tmp_file, write_back=grey.WriteBack.YES))
            with open(tmp_file, encoding="utf8") as f:
                actual = f.read()
        finally:
            os.unlink(tmp_file)
        self.assertFormatEqual(expected, actual)
        with patch("grey.dump_to_file", dump_to_stderr):
            grey.assert_equivalent(source, actual)
            grey.assert_stable(source, actual, DEFAULT_MODE)

    def test_expression_diff(self) -> None:
        source, _ = read_data("simple_cases", "expression.py")
        expected, _ = read_data("simple_cases", "expression.diff")
        tmp_file = Path(grey.dump_to_file(source))
        diff_header = re.compile(
            rf"{re.escape(str(tmp_file))}\t\d\d\d\d-\d\d-\d\d "
            r"\d\d:\d\d:\d\d\.\d\d\d\d\d\d \+\d\d\d\d"
        )
        try:
            result = BlackRunner().invoke(
                grey.main, ["--diff", str(tmp_file), f"--config={EMPTY_CONFIG}"]
            )
            self.assertEqual(result.exit_code, 0)
        finally:
            os.unlink(tmp_file)
        actual = result.output
        actual = diff_header.sub(DETERMINISTIC_HEADER, actual)
        if expected != actual:
            dump = grey.dump_to_file(actual)
            msg = (
                "Expected diff isn't equal to the actual. If you made changes to"
                " expression.py and this is an anticipated difference, overwrite"
                f" tests/data/expression.diff with {dump}"
            )
            self.assertEqual(expected, actual, msg)

    def test_expression_diff_with_color(self) -> None:
        source, _ = read_data("simple_cases", "expression.py")
        expected, _ = read_data("simple_cases", "expression.diff")
        tmp_file = Path(grey.dump_to_file(source))
        try:
            result = BlackRunner().invoke(
                grey.main,
                ["--diff", "--color", str(tmp_file), f"--config={EMPTY_CONFIG}"],
            )
        finally:
            os.unlink(tmp_file)
        actual = result.output
        # We check the contents of the diff in `test_expression_diff`. All
        # we need to check here is that color codes exist in the result.
        self.assertIn("\033[1m", actual)
        self.assertIn("\033[36m", actual)
        self.assertIn("\033[32m", actual)
        self.assertIn("\033[31m", actual)
        self.assertIn("\033[0m", actual)

    def test_detect_pos_only_arguments(self) -> None:
        source, _ = read_data("py_38", "pep_570")
        root = grey.lib2to3_parse(source)
        features = grey.get_features_used(root)
        self.assertIn(grey.Feature.POS_ONLY_ARGUMENTS, features)
        versions = grey.detect_target_versions(root)
        self.assertIn(grey.TargetVersion.PY38, versions)

    @patch("grey.dump_to_file", dump_to_stderr)
    def test_string_quotes(self) -> None:
        source, expected = read_data("miscellaneous", "string_quotes")
        mode = grey.Mode(preview=True)
        assert_format(source, expected, mode)
        mode = replace(mode, string_normalization=False)
        not_normalized = fs(source, mode=mode)
        self.assertFormatEqual(source.replace("\\\n", ""), not_normalized)
        grey.assert_equivalent(source, not_normalized)
        grey.assert_stable(source, not_normalized, mode=mode)

    def test_skip_magic_trailing_comma(self) -> None:
        source, _ = read_data("simple_cases", "expression")
        expected, _ = read_data(
            "miscellaneous", "expression_skip_magic_trailing_comma.diff"
        )
        tmp_file = Path(grey.dump_to_file(source))
        diff_header = re.compile(
            rf"{re.escape(str(tmp_file))}\t\d\d\d\d-\d\d-\d\d "
            r"\d\d:\d\d:\d\d\.\d\d\d\d\d\d \+\d\d\d\d"
        )
        try:
            result = BlackRunner().invoke(
                grey.main,
                ["-C", "--diff", str(tmp_file), f"--config={EMPTY_CONFIG}"],
            )
            self.assertEqual(result.exit_code, 0)
        finally:
            os.unlink(tmp_file)
        actual = result.output
        actual = diff_header.sub(DETERMINISTIC_HEADER, actual)
        actual = actual.rstrip() + "\n"  # the diff output has a trailing space
        if expected != actual:
            dump = grey.dump_to_file(actual)
            msg = (
                "Expected diff isn't equal to the actual. If you made changes to"
                " expression.py and this is an anticipated difference, overwrite"
                f" tests/data/expression_skip_magic_trailing_comma.diff with {dump}"
            )
            self.assertEqual(expected, actual, msg)

    @patch("grey.dump_to_file", dump_to_stderr)
    def test_async_as_identifier(self) -> None:
        source_path = get_case_path("miscellaneous", "async_as_identifier")
        source, expected = read_data_from_file(source_path)
        actual = fs(source)
        self.assertFormatEqual(expected, actual)
        major, minor = sys.version_info[:2]
        if major < 3 or (major <= 3 and minor < 7):
            grey.assert_equivalent(source, actual)
        grey.assert_stable(source, actual, DEFAULT_MODE)
        # ensure grey can parse this when the target is 3.6
        self.invokeBlack([str(source_path), "--target-version", "py36"])
        # but not on 3.7, because async/await is no longer an identifier
        self.invokeBlack([str(source_path), "--target-version", "py37"], exit_code=123)

    @patch("grey.dump_to_file", dump_to_stderr)
    def test_python37(self) -> None:
        source_path = get_case_path("py_37", "python37")
        source, expected = read_data_from_file(source_path)
        actual = fs(source)
        self.assertFormatEqual(expected, actual)
        major, minor = sys.version_info[:2]
        if major > 3 or (major == 3 and minor >= 7):
            grey.assert_equivalent(source, actual)
        grey.assert_stable(source, actual, DEFAULT_MODE)
        # ensure grey can parse this when the target is 3.7
        self.invokeBlack([str(source_path), "--target-version", "py37"])
        # but not on 3.6, because we use async as a reserved keyword
        self.invokeBlack([str(source_path), "--target-version", "py36"], exit_code=123)

    def test_tab_comment_indentation(self) -> None:
        contents_tab = "if 1:\n\tif 2:\n\t\tpass\n\t# comment\n\tpass\n"
        contents_spc = "if 1:\n    if 2:\n        pass\n    # comment\n    pass\n"
        self.assertFormatEqual(contents_spc, fs(contents_spc))
        self.assertFormatEqual(contents_spc, fs(contents_tab))

        contents_tab = "if 1:\n\tif 2:\n\t\tpass\n\t\t# comment\n\tpass\n"
        contents_spc = "if 1:\n    if 2:\n        pass\n        # comment\n    pass\n"
        self.assertFormatEqual(contents_spc, fs(contents_spc))
        self.assertFormatEqual(contents_spc, fs(contents_tab))

        # mixed tabs and spaces (valid Python 2 code)
        contents_tab = "if 1:\n        if 2:\n\t\tpass\n\t# comment\n        pass\n"
        contents_spc = "if 1:\n    if 2:\n        pass\n    # comment\n    pass\n"
        self.assertFormatEqual(contents_spc, fs(contents_spc))
        self.assertFormatEqual(contents_spc, fs(contents_tab))

        contents_tab = "if 1:\n        if 2:\n\t\tpass\n\t\t# comment\n        pass\n"
        contents_spc = "if 1:\n    if 2:\n        pass\n        # comment\n    pass\n"
        self.assertFormatEqual(contents_spc, fs(contents_spc))
        self.assertFormatEqual(contents_spc, fs(contents_tab))

    def test_report_verbose(self) -> None:
        report = Report(verbose=True)
        out_lines = []
        err_lines = []

        def out(msg: str, **kwargs: Any) -> None:
            out_lines.append(msg)

        def err(msg: str, **kwargs: Any) -> None:
            err_lines.append(msg)

        with patch("grey.output._out", out), patch("grey.output._err", err):
            report.done(Path("f1"), grey.Changed.NO)
            self.assertEqual(len(out_lines), 1)
            self.assertEqual(len(err_lines), 0)
            self.assertEqual(out_lines[-1], "f1 already well formatted, good job.")
            self.assertEqual(unstyle(str(report)), "1 file left unchanged.")
            self.assertEqual(report.return_code, 0)
            report.done(Path("f2"), grey.Changed.YES)
            self.assertEqual(len(out_lines), 2)
            self.assertEqual(len(err_lines), 0)
            self.assertEqual(out_lines[-1], "reformatted f2")
            self.assertEqual(
                unstyle(str(report)), "1 file reformatted, 1 file left unchanged."
            )
            report.done(Path("f3"), grey.Changed.CACHED)
            self.assertEqual(len(out_lines), 3)
            self.assertEqual(len(err_lines), 0)
            self.assertEqual(
                out_lines[-1], "f3 wasn't modified on disk since last run."
            )
            self.assertEqual(
                unstyle(str(report)), "1 file reformatted, 2 files left unchanged."
            )
            self.assertEqual(report.return_code, 0)
            report.check = True
            self.assertEqual(report.return_code, 1)
            report.check = False
            report.failed(Path("e1"), "boom")
            self.assertEqual(len(out_lines), 3)
            self.assertEqual(len(err_lines), 1)
            self.assertEqual(err_lines[-1], "error: cannot format e1: boom")
            self.assertEqual(
                unstyle(str(report)),
                "1 file reformatted, 2 files left unchanged, 1 file failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.done(Path("f3"), grey.Changed.YES)
            self.assertEqual(len(out_lines), 4)
            self.assertEqual(len(err_lines), 1)
            self.assertEqual(out_lines[-1], "reformatted f3")
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 2 files left unchanged, 1 file failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.failed(Path("e2"), "boom")
            self.assertEqual(len(out_lines), 4)
            self.assertEqual(len(err_lines), 2)
            self.assertEqual(err_lines[-1], "error: cannot format e2: boom")
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 2 files left unchanged, 2 files failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.path_ignored(Path("wat"), "no match")
            self.assertEqual(len(out_lines), 5)
            self.assertEqual(len(err_lines), 2)
            self.assertEqual(out_lines[-1], "wat ignored: no match")
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 2 files left unchanged, 2 files failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.done(Path("f4"), grey.Changed.NO)
            self.assertEqual(len(out_lines), 6)
            self.assertEqual(len(err_lines), 2)
            self.assertEqual(out_lines[-1], "f4 already well formatted, good job.")
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 3 files left unchanged, 2 files failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.check = True
            self.assertEqual(
                unstyle(str(report)),
                "2 files would be reformatted, 3 files would be left unchanged, 2 files"
                " would fail to reformat.",
            )
            report.check = False
            report.diff = True
            self.assertEqual(
                unstyle(str(report)),
                "2 files would be reformatted, 3 files would be left unchanged, 2 files"
                " would fail to reformat.",
            )

    def test_report_quiet(self) -> None:
        report = Report(quiet=True)
        out_lines = []
        err_lines = []

        def out(msg: str, **kwargs: Any) -> None:
            out_lines.append(msg)

        def err(msg: str, **kwargs: Any) -> None:
            err_lines.append(msg)

        with patch("grey.output._out", out), patch("grey.output._err", err):
            report.done(Path("f1"), grey.Changed.NO)
            self.assertEqual(len(out_lines), 0)
            self.assertEqual(len(err_lines), 0)
            self.assertEqual(unstyle(str(report)), "1 file left unchanged.")
            self.assertEqual(report.return_code, 0)
            report.done(Path("f2"), grey.Changed.YES)
            self.assertEqual(len(out_lines), 0)
            self.assertEqual(len(err_lines), 0)
            self.assertEqual(
                unstyle(str(report)), "1 file reformatted, 1 file left unchanged."
            )
            report.done(Path("f3"), grey.Changed.CACHED)
            self.assertEqual(len(out_lines), 0)
            self.assertEqual(len(err_lines), 0)
            self.assertEqual(
                unstyle(str(report)), "1 file reformatted, 2 files left unchanged."
            )
            self.assertEqual(report.return_code, 0)
            report.check = True
            self.assertEqual(report.return_code, 1)
            report.check = False
            report.failed(Path("e1"), "boom")
            self.assertEqual(len(out_lines), 0)
            self.assertEqual(len(err_lines), 1)
            self.assertEqual(err_lines[-1], "error: cannot format e1: boom")
            self.assertEqual(
                unstyle(str(report)),
                "1 file reformatted, 2 files left unchanged, 1 file failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.done(Path("f3"), grey.Changed.YES)
            self.assertEqual(len(out_lines), 0)
            self.assertEqual(len(err_lines), 1)
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 2 files left unchanged, 1 file failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.failed(Path("e2"), "boom")
            self.assertEqual(len(out_lines), 0)
            self.assertEqual(len(err_lines), 2)
            self.assertEqual(err_lines[-1], "error: cannot format e2: boom")
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 2 files left unchanged, 2 files failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.path_ignored(Path("wat"), "no match")
            self.assertEqual(len(out_lines), 0)
            self.assertEqual(len(err_lines), 2)
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 2 files left unchanged, 2 files failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.done(Path("f4"), grey.Changed.NO)
            self.assertEqual(len(out_lines), 0)
            self.assertEqual(len(err_lines), 2)
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 3 files left unchanged, 2 files failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.check = True
            self.assertEqual(
                unstyle(str(report)),
                "2 files would be reformatted, 3 files would be left unchanged, 2 files"
                " would fail to reformat.",
            )
            report.check = False
            report.diff = True
            self.assertEqual(
                unstyle(str(report)),
                "2 files would be reformatted, 3 files would be left unchanged, 2 files"
                " would fail to reformat.",
            )

    def test_report_normal(self) -> None:
        report = grey.Report()
        out_lines = []
        err_lines = []

        def out(msg: str, **kwargs: Any) -> None:
            out_lines.append(msg)

        def err(msg: str, **kwargs: Any) -> None:
            err_lines.append(msg)

        with patch("grey.output._out", out), patch("grey.output._err", err):
            report.done(Path("f1"), grey.Changed.NO)
            self.assertEqual(len(out_lines), 0)
            self.assertEqual(len(err_lines), 0)
            self.assertEqual(unstyle(str(report)), "1 file left unchanged.")
            self.assertEqual(report.return_code, 0)
            report.done(Path("f2"), grey.Changed.YES)
            self.assertEqual(len(out_lines), 1)
            self.assertEqual(len(err_lines), 0)
            self.assertEqual(out_lines[-1], "reformatted f2")
            self.assertEqual(
                unstyle(str(report)), "1 file reformatted, 1 file left unchanged."
            )
            report.done(Path("f3"), grey.Changed.CACHED)
            self.assertEqual(len(out_lines), 1)
            self.assertEqual(len(err_lines), 0)
            self.assertEqual(out_lines[-1], "reformatted f2")
            self.assertEqual(
                unstyle(str(report)), "1 file reformatted, 2 files left unchanged."
            )
            self.assertEqual(report.return_code, 0)
            report.check = True
            self.assertEqual(report.return_code, 1)
            report.check = False
            report.failed(Path("e1"), "boom")
            self.assertEqual(len(out_lines), 1)
            self.assertEqual(len(err_lines), 1)
            self.assertEqual(err_lines[-1], "error: cannot format e1: boom")
            self.assertEqual(
                unstyle(str(report)),
                "1 file reformatted, 2 files left unchanged, 1 file failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.done(Path("f3"), grey.Changed.YES)
            self.assertEqual(len(out_lines), 2)
            self.assertEqual(len(err_lines), 1)
            self.assertEqual(out_lines[-1], "reformatted f3")
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 2 files left unchanged, 1 file failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.failed(Path("e2"), "boom")
            self.assertEqual(len(out_lines), 2)
            self.assertEqual(len(err_lines), 2)
            self.assertEqual(err_lines[-1], "error: cannot format e2: boom")
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 2 files left unchanged, 2 files failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.path_ignored(Path("wat"), "no match")
            self.assertEqual(len(out_lines), 2)
            self.assertEqual(len(err_lines), 2)
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 2 files left unchanged, 2 files failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.done(Path("f4"), grey.Changed.NO)
            self.assertEqual(len(out_lines), 2)
            self.assertEqual(len(err_lines), 2)
            self.assertEqual(
                unstyle(str(report)),
                "2 files reformatted, 3 files left unchanged, 2 files failed to"
                " reformat.",
            )
            self.assertEqual(report.return_code, 123)
            report.check = True
            self.assertEqual(
                unstyle(str(report)),
                "2 files would be reformatted, 3 files would be left unchanged, 2 files"
                " would fail to reformat.",
            )
            report.check = False
            report.diff = True
            self.assertEqual(
                unstyle(str(report)),
                "2 files would be reformatted, 3 files would be left unchanged, 2 files"
                " would fail to reformat.",
            )

    def test_lib2to3_parse(self) -> None:
        with self.assertRaises(grey.InvalidInput):
            grey.lib2to3_parse("invalid syntax")

        straddling = "x + y"
        grey.lib2to3_parse(straddling)
        grey.lib2to3_parse(straddling, {TargetVersion.PY36})

        py2_only = "print x"
        with self.assertRaises(grey.InvalidInput):
            grey.lib2to3_parse(py2_only, {TargetVersion.PY36})

        py3_only = "exec(x, end=y)"
        grey.lib2to3_parse(py3_only)
        grey.lib2to3_parse(py3_only, {TargetVersion.PY36})

    def test_get_features_used_decorator(self) -> None:
        # Test the feature detection of new decorator syntax
        # since this makes some test cases of test_get_features_used()
        # fails if it fails, this is tested first so that a useful case
        # is identified
        simples, relaxed = read_data("miscellaneous", "decorators")
        # skip explanation comments at the top of the file
        for simple_test in simples.split("##")[1:]:
            node = grey.lib2to3_parse(simple_test)
            decorator = str(node.children[0].children[0]).strip()
            self.assertNotIn(
                Feature.RELAXED_DECORATORS,
                grey.get_features_used(node),
                msg=(
                    f"decorator '{decorator}' follows python<=3.8 syntax"
                    "but is detected as 3.9+"
                    # f"The full node is\n{node!r}"
                ),
            )
        # skip the '# output' comment at the top of the output part
        for relaxed_test in relaxed.split("##")[1:]:
            node = grey.lib2to3_parse(relaxed_test)
            decorator = str(node.children[0].children[0]).strip()
            self.assertIn(
                Feature.RELAXED_DECORATORS,
                grey.get_features_used(node),
                msg=(
                    f"decorator '{decorator}' uses python3.9+ syntax"
                    "but is detected as python<=3.8"
                    # f"The full node is\n{node!r}"
                ),
            )

    def test_get_features_used(self) -> None:
        node = grey.lib2to3_parse("def f(*, arg): ...\n")
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse("def f(*, arg,): ...\n")
        self.assertEqual(
            grey.get_features_used(node), {Feature.TRAILING_COMMA_IN_DEF}
        )
        node = grey.lib2to3_parse("f(*arg,)\n")
        self.assertEqual(
            grey.get_features_used(node), {Feature.TRAILING_COMMA_IN_CALL}
        )
        node = grey.lib2to3_parse("def f(*, arg): f'string'\n")
        self.assertEqual(grey.get_features_used(node), {Feature.F_STRINGS})
        node = grey.lib2to3_parse("123_456\n")
        self.assertEqual(
            grey.get_features_used(node), {Feature.NUMERIC_UNDERSCORES}
        )
        node = grey.lib2to3_parse("123456\n")
        self.assertEqual(grey.get_features_used(node), set())
        source, expected = read_data("simple_cases", "function")
        node = grey.lib2to3_parse(source)
        expected_features = {
            Feature.TRAILING_COMMA_IN_CALL,
            Feature.TRAILING_COMMA_IN_DEF,
            Feature.F_STRINGS,
        }
        self.assertEqual(grey.get_features_used(node), expected_features)
        node = grey.lib2to3_parse(expected)
        self.assertEqual(grey.get_features_used(node), expected_features)
        source, expected = read_data("simple_cases", "expression")
        node = grey.lib2to3_parse(source)
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse(expected)
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse("lambda a, /, b: ...")
        self.assertEqual(grey.get_features_used(node), {Feature.POS_ONLY_ARGUMENTS})
        node = grey.lib2to3_parse("def fn(a, /, b): ...")
        self.assertEqual(grey.get_features_used(node), {Feature.POS_ONLY_ARGUMENTS})
        node = grey.lib2to3_parse("def fn(): yield a, b")
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse("def fn(): return a, b")
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse("def fn(): yield *b, c")
        self.assertEqual(grey.get_features_used(node), {Feature.UNPACKING_ON_FLOW})
        node = grey.lib2to3_parse("def fn(): return a, *b, c")
        self.assertEqual(grey.get_features_used(node), {Feature.UNPACKING_ON_FLOW})
        node = grey.lib2to3_parse("x = a, *b, c")
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse("x: Any = regular")
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse("x: Any = (regular, regular)")
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse("x: Any = Complex(Type(1))[something]")
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse("x: Tuple[int, ...] = a, b, c")
        self.assertEqual(
            grey.get_features_used(node), {Feature.ANN_ASSIGN_EXTENDED_RHS}
        )
        node = grey.lib2to3_parse("try: pass\nexcept Something: pass")
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse("try: pass\nexcept (*Something,): pass")
        self.assertEqual(grey.get_features_used(node), set())
        node = grey.lib2to3_parse("try: pass\nexcept *Group: pass")
        self.assertEqual(grey.get_features_used(node), {Feature.EXCEPT_STAR})
        node = grey.lib2to3_parse("a[*b]")
        self.assertEqual(grey.get_features_used(node), {Feature.VARIADIC_GENERICS})
        node = grey.lib2to3_parse("a[x, *y(), z] = t")
        self.assertEqual(grey.get_features_used(node), {Feature.VARIADIC_GENERICS})
        node = grey.lib2to3_parse("def fn(*args: *T): pass")
        self.assertEqual(grey.get_features_used(node), {Feature.VARIADIC_GENERICS})

    def test_get_features_used_for_future_flags(self) -> None:
        for src, features in [
            ("from __future__ import annotations", {Feature.FUTURE_ANNOTATIONS}),
            (
                "from __future__ import (other, annotations)",
                {Feature.FUTURE_ANNOTATIONS},
            ),
            ("a = 1 + 2\nfrom something import annotations", set()),
            ("from __future__ import x, y", set()),
        ]:
            with self.subTest(src=src, features=features):
                node = grey.lib2to3_parse(src)
                future_imports = grey.get_future_imports(node)
                self.assertEqual(
                    grey.get_features_used(node, future_imports=future_imports),
                    features,
                )

    def test_get_future_imports(self) -> None:
        node = grey.lib2to3_parse("\n")
        self.assertEqual(set(), grey.get_future_imports(node))
        node = grey.lib2to3_parse("from __future__ import grey\n")
        self.assertEqual({"grey"}, grey.get_future_imports(node))
        node = grey.lib2to3_parse("from __future__ import multiple, imports\n")
        self.assertEqual({"multiple", "imports"}, grey.get_future_imports(node))
        node = grey.lib2to3_parse(
            "from __future__ import (parenthesized, imports)\n"
        )
        self.assertEqual(
            {"parenthesized", "imports"}, grey.get_future_imports(node)
        )
        node = grey.lib2to3_parse(
            "from __future__ import multiple\nfrom __future__ import imports\n"
        )
        self.assertEqual({"multiple", "imports"}, grey.get_future_imports(node))
        node = grey.lib2to3_parse("# comment\nfrom __future__ import grey\n")
        self.assertEqual({"grey"}, grey.get_future_imports(node))
        node = grey.lib2to3_parse(
            '"""docstring"""\nfrom __future__ import grey\n'
        )
        self.assertEqual({"grey"}, grey.get_future_imports(node))
        node = grey.lib2to3_parse(
            "some(other, code)\nfrom __future__ import grey\n"
        )
        self.assertEqual(set(), grey.get_future_imports(node))
        node = grey.lib2to3_parse("from some.module import grey\n")
        self.assertEqual(set(), grey.get_future_imports(node))
        node = grey.lib2to3_parse(
            "from __future__ import unicode_literals as _unicode_literals"
        )
        self.assertEqual({"unicode_literals"}, grey.get_future_imports(node))
        node = grey.lib2to3_parse(
            "from __future__ import unicode_literals as _lol, print"
        )
        self.assertEqual(
            {"unicode_literals", "print"}, grey.get_future_imports(node)
        )

    @pytest.mark.incompatible_with_mypyc
    def test_debug_visitor(self) -> None:
        source, _ = read_data("miscellaneous", "debug_visitor")
        expected, _ = read_data("miscellaneous", "debug_visitor.out")
        out_lines = []
        err_lines = []

        def out(msg: str, **kwargs: Any) -> None:
            out_lines.append(msg)

        def err(msg: str, **kwargs: Any) -> None:
            err_lines.append(msg)

        with patch("grey.debug.out", out):
            DebugVisitor.show(source)
        actual = "\n".join(out_lines) + "\n"
        log_name = ""
        if expected != actual:
            log_name = grey.dump_to_file(*out_lines)
        self.assertEqual(
            expected,
            actual,
            f"AST print out is different. Actual version dumped to {log_name}",
        )

    def test_format_file_contents(self) -> None:
        empty = ""
        mode = DEFAULT_MODE
        with self.assertRaises(grey.NothingChanged):
            grey.format_file_contents(empty, mode=mode, fast=False)
        just_nl = "\n"
        with self.assertRaises(grey.NothingChanged):
            grey.format_file_contents(just_nl, mode=mode, fast=False)
        same = "j = [1, 2, 3]\n"
        with self.assertRaises(grey.NothingChanged):
            grey.format_file_contents(same, mode=mode, fast=False)
        different = "j = [1,2,3]"
        expected = same
        actual = grey.format_file_contents(different, mode=mode, fast=False)
        self.assertEqual(expected, actual)
        invalid = "return if you can"
        with self.assertRaises(grey.InvalidInput) as e:
            grey.format_file_contents(invalid, mode=mode, fast=False)
        self.assertEqual(str(e.exception), "Cannot parse: 1:7: return if you can")

    def test_endmarker(self) -> None:
        n = grey.lib2to3_parse("\n")
        self.assertEqual(n.type, grey.syms.file_input)
        self.assertEqual(len(n.children), 1)
        self.assertEqual(n.children[0].type, grey.token.ENDMARKER)

    @pytest.mark.incompatible_with_mypyc
    @unittest.skipIf(os.environ.get("SKIP_AST_PRINT"), "user set SKIP_AST_PRINT")
    def test_assertFormatEqual(self) -> None:
        out_lines = []
        err_lines = []

        def out(msg: str, **kwargs: Any) -> None:
            out_lines.append(msg)

        def err(msg: str, **kwargs: Any) -> None:
            err_lines.append(msg)

        with patch("grey.output._out", out), patch("grey.output._err", err):
            with self.assertRaises(AssertionError):
                self.assertFormatEqual("j = [1, 2, 3]", "j = [1, 2, 3,]")

        out_str = "".join(out_lines)
        self.assertIn("Expected tree:", out_str)
        self.assertIn("Actual tree:", out_str)
        self.assertEqual("".join(err_lines), "")

    @event_loop()
    @patch("concurrent.futures.ProcessPoolExecutor", MagicMock(side_effect=OSError))
    def test_works_in_mono_process_only_environment(self) -> None:
        with cache_dir() as workspace:
            for f in [
                (workspace / "one.py").resolve(),
                (workspace / "two.py").resolve(),
            ]:
                f.write_text('print("hello")\n')
            self.invokeBlack([str(workspace)])

    @event_loop()
    def test_check_diff_use_together(self) -> None:
        with cache_dir():
            # Files which will be reformatted.
            src1 = get_case_path("miscellaneous", "string_quotes")
            self.invokeBlack([str(src1), "--diff", "--check"], exit_code=1)
            # Files which will not be reformatted.
            src2 = get_case_path("simple_cases", "composition")
            self.invokeBlack([str(src2), "--diff", "--check"])
            # Multi file command.
            self.invokeBlack([str(src1), str(src2), "--diff", "--check"], exit_code=1)

    def test_no_src_fails(self) -> None:
        with cache_dir():
            self.invokeBlack([], exit_code=1)

    def test_src_and_code_fails(self) -> None:
        with cache_dir():
            self.invokeBlack([".", "-c", "0"], exit_code=1)

    def test_broken_symlink(self) -> None:
        with cache_dir() as workspace:
            symlink = workspace / "broken_link.py"
            try:
                symlink.symlink_to("nonexistent.py")
            except (OSError, NotImplementedError) as e:
                self.skipTest(f"Can't create symlinks: {e}")
            self.invokeBlack([str(workspace.resolve())])

    def test_single_file_force_pyi(self) -> None:
        pyi_mode = replace(DEFAULT_MODE, is_pyi=True)
        contents, expected = read_data("miscellaneous", "force_pyi")
        with cache_dir() as workspace:
            path = (workspace / "file.py").resolve()
            with open(path, "w") as fh:
                fh.write(contents)
            self.invokeBlack([str(path), "--pyi"])
            with open(path, "r") as fh:
                actual = fh.read()
            # verify cache with --pyi is separate
            pyi_cache = grey.read_cache(pyi_mode)
            self.assertIn(str(path), pyi_cache)
            normal_cache = grey.read_cache(DEFAULT_MODE)
            self.assertNotIn(str(path), normal_cache)
        self.assertFormatEqual(expected, actual)
        grey.assert_equivalent(contents, actual)
        grey.assert_stable(contents, actual, pyi_mode)

    @event_loop()
    def test_multi_file_force_pyi(self) -> None:
        reg_mode = DEFAULT_MODE
        pyi_mode = replace(DEFAULT_MODE, is_pyi=True)
        contents, expected = read_data("miscellaneous", "force_pyi")
        with cache_dir() as workspace:
            paths = [
                (workspace / "file1.py").resolve(),
                (workspace / "file2.py").resolve(),
            ]
            for path in paths:
                with open(path, "w") as fh:
                    fh.write(contents)
            self.invokeBlack([str(p) for p in paths] + ["--pyi"])
            for path in paths:
                with open(path, "r") as fh:
                    actual = fh.read()
                self.assertEqual(actual, expected)
            # verify cache with --pyi is separate
            pyi_cache = grey.read_cache(pyi_mode)
            normal_cache = grey.read_cache(reg_mode)
            for path in paths:
                self.assertIn(str(path), pyi_cache)
                self.assertNotIn(str(path), normal_cache)

    def test_pipe_force_pyi(self) -> None:
        source, expected = read_data("miscellaneous", "force_pyi")
        result = CliRunner().invoke(
            grey.main, ["-", "-q", "--pyi"], input=BytesIO(source.encode("utf8"))
        )
        self.assertEqual(result.exit_code, 0)
        actual = result.output
        self.assertFormatEqual(actual, expected)

    def test_single_file_force_py36(self) -> None:
        reg_mode = DEFAULT_MODE
        py36_mode = replace(DEFAULT_MODE, target_versions=PY36_VERSIONS)
        source, expected = read_data("miscellaneous", "force_py36")
        with cache_dir() as workspace:
            path = (workspace / "file.py").resolve()
            with open(path, "w") as fh:
                fh.write(source)
            self.invokeBlack([str(path), *PY36_ARGS])
            with open(path, "r") as fh:
                actual = fh.read()
            # verify cache with --target-version is separate
            py36_cache = grey.read_cache(py36_mode)
            self.assertIn(str(path), py36_cache)
            normal_cache = grey.read_cache(reg_mode)
            self.assertNotIn(str(path), normal_cache)
        self.assertEqual(actual, expected)

    @event_loop()
    def test_multi_file_force_py36(self) -> None:
        reg_mode = DEFAULT_MODE
        py36_mode = replace(DEFAULT_MODE, target_versions=PY36_VERSIONS)
        source, expected = read_data("miscellaneous", "force_py36")
        with cache_dir() as workspace:
            paths = [
                (workspace / "file1.py").resolve(),
                (workspace / "file2.py").resolve(),
            ]
            for path in paths:
                with open(path, "w") as fh:
                    fh.write(source)
            self.invokeBlack([str(p) for p in paths] + PY36_ARGS)
            for path in paths:
                with open(path, "r") as fh:
                    actual = fh.read()
                self.assertEqual(actual, expected)
            # verify cache with --target-version is separate
            pyi_cache = grey.read_cache(py36_mode)
            normal_cache = grey.read_cache(reg_mode)
            for path in paths:
                self.assertIn(str(path), pyi_cache)
                self.assertNotIn(str(path), normal_cache)

    def test_pipe_force_py36(self) -> None:
        source, expected = read_data("miscellaneous", "force_py36")
        result = CliRunner().invoke(
            grey.main,
            ["-", "-q", "--target-version=py36"],
            input=BytesIO(source.encode("utf8")),
        )
        self.assertEqual(result.exit_code, 0)
        actual = result.output
        self.assertFormatEqual(actual, expected)

    @pytest.mark.incompatible_with_mypyc
    def test_reformat_one_with_stdin(self) -> None:
        with patch(
            "grey.format_stdin_to_stdout",
            return_value=lambda *args, **kwargs: grey.Changed.YES,
        ) as fsts:
            report = MagicMock()
            path = Path("-")
            grey.reformat_one(
                path,
                fast=True,
                write_back=grey.WriteBack.YES,
                mode=DEFAULT_MODE,
                report=report,
            )
            fsts.assert_called_once()
            report.done.assert_called_with(path, grey.Changed.YES)

    @pytest.mark.incompatible_with_mypyc
    def test_reformat_one_with_stdin_filename(self) -> None:
        with patch(
            "grey.format_stdin_to_stdout",
            return_value=lambda *args, **kwargs: grey.Changed.YES,
        ) as fsts:
            report = MagicMock()
            p = "foo.py"
            path = Path(f"__BLACK_STDIN_FILENAME__{p}")
            expected = Path(p)
            grey.reformat_one(
                path,
                fast=True,
                write_back=grey.WriteBack.YES,
                mode=DEFAULT_MODE,
                report=report,
            )
            fsts.assert_called_once_with(
                fast=True, write_back=grey.WriteBack.YES, mode=DEFAULT_MODE
            )
            # __BLACK_STDIN_FILENAME__ should have been stripped
            report.done.assert_called_with(expected, grey.Changed.YES)

    @pytest.mark.incompatible_with_mypyc
    def test_reformat_one_with_stdin_filename_pyi(self) -> None:
        with patch(
            "grey.format_stdin_to_stdout",
            return_value=lambda *args, **kwargs: grey.Changed.YES,
        ) as fsts:
            report = MagicMock()
            p = "foo.pyi"
            path = Path(f"__BLACK_STDIN_FILENAME__{p}")
            expected = Path(p)
            grey.reformat_one(
                path,
                fast=True,
                write_back=grey.WriteBack.YES,
                mode=DEFAULT_MODE,
                report=report,
            )
            fsts.assert_called_once_with(
                fast=True,
                write_back=grey.WriteBack.YES,
                mode=replace(DEFAULT_MODE, is_pyi=True),
            )
            # __BLACK_STDIN_FILENAME__ should have been stripped
            report.done.assert_called_with(expected, grey.Changed.YES)

    @pytest.mark.incompatible_with_mypyc
    def test_reformat_one_with_stdin_filename_ipynb(self) -> None:
        with patch(
            "grey.format_stdin_to_stdout",
            return_value=lambda *args, **kwargs: grey.Changed.YES,
        ) as fsts:
            report = MagicMock()
            p = "foo.ipynb"
            path = Path(f"__BLACK_STDIN_FILENAME__{p}")
            expected = Path(p)
            grey.reformat_one(
                path,
                fast=True,
                write_back=grey.WriteBack.YES,
                mode=DEFAULT_MODE,
                report=report,
            )
            fsts.assert_called_once_with(
                fast=True,
                write_back=grey.WriteBack.YES,
                mode=replace(DEFAULT_MODE, is_ipynb=True),
            )
            # __BLACK_STDIN_FILENAME__ should have been stripped
            report.done.assert_called_with(expected, grey.Changed.YES)

    @pytest.mark.incompatible_with_mypyc
    def test_reformat_one_with_stdin_and_existing_path(self) -> None:
        with patch(
            "grey.format_stdin_to_stdout",
            return_value=lambda *args, **kwargs: grey.Changed.YES,
        ) as fsts:
            report = MagicMock()
            # Even with an existing file, since we are forcing stdin, grey
            # should output to stdout and not modify the file inplace
            p = THIS_DIR / "data" / "simple_cases" / "collections.py"
            # Make sure is_file actually returns True
            self.assertTrue(p.is_file())
            path = Path(f"__BLACK_STDIN_FILENAME__{p}")
            expected = Path(p)
            grey.reformat_one(
                path,
                fast=True,
                write_back=grey.WriteBack.YES,
                mode=DEFAULT_MODE,
                report=report,
            )
            fsts.assert_called_once()
            # __BLACK_STDIN_FILENAME__ should have been stripped
            report.done.assert_called_with(expected, grey.Changed.YES)

    def test_reformat_one_with_stdin_empty(self) -> None:
        output = io.StringIO()
        with patch("io.TextIOWrapper", lambda *args, **kwargs: output):
            try:
                grey.format_stdin_to_stdout(
                    fast=True,
                    content="",
                    write_back=grey.WriteBack.YES,
                    mode=DEFAULT_MODE,
                )
            except io.UnsupportedOperation:
                pass  # StringIO does not support detach
            assert output.getvalue() == ""

    def test_invalid_cli_regex(self) -> None:
        for option in ["--include", "--exclude", "--extend-exclude", "--force-exclude"]:
            self.invokeBlack(["-", option, "**()(!!*)"], exit_code=2)

    def test_required_version_matches_version(self) -> None:
        self.invokeBlack(
            ["--required-version", grey.__version__, "-c", "0"],
            exit_code=0,
            ignore_config=True,
        )

    def test_required_version_matches_partial_version(self) -> None:
        self.invokeBlack(
            ["--required-version", grey.__version__.split(".")[0], "-c", "0"],
            exit_code=0,
            ignore_config=True,
        )

    def test_required_version_does_not_match_on_minor_version(self) -> None:
        self.invokeBlack(
            [
                "--required-version",
                grey.__version__.split(".")[0] + ".999",
                "-c",
                "0",
            ],
            exit_code=1,
            ignore_config=True,
        )

    def test_required_version_does_not_match_version(self) -> None:
        result = BlackRunner().invoke(
            grey.main,
            ["--required-version", "20.99b", "-c", "0"],
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("required version", result.stderr)

    def test_preserves_line_endings(self) -> None:
        with TemporaryDirectory() as workspace:
            test_file = Path(workspace) / "test.py"
            for nl in ["\n", "\r\n"]:
                contents = nl.join(["def f(  ):", "    pass"])
                test_file.write_bytes(contents.encode())
                ff(test_file, write_back=grey.WriteBack.YES)
                updated_contents: bytes = test_file.read_bytes()
                self.assertIn(nl.encode(), updated_contents)
                if nl == "\n":
                    self.assertNotIn(b"\r\n", updated_contents)

    def test_preserves_line_endings_via_stdin(self) -> None:
        for nl in ["\n", "\r\n"]:
            contents = nl.join(["def f(  ):", "    pass"])
            runner = BlackRunner()
            result = runner.invoke(
                grey.main, ["-", "--fast"], input=BytesIO(contents.encode("utf8"))
            )
            self.assertEqual(result.exit_code, 0)
            output = result.stdout_bytes
            self.assertIn(nl.encode("utf8"), output)
            if nl == "\n":
                self.assertNotIn(b"\r\n", output)

    def test_assert_equivalent_different_asts(self) -> None:
        with self.assertRaises(AssertionError):
            grey.assert_equivalent("{}", "None")

    def test_shhh_click(self) -> None:
        try:
            from click import _unicodefun  # type: ignore
        except ImportError:
            self.skipTest("Incompatible Click version")

        if not hasattr(_unicodefun, "_verify_python_env"):
            self.skipTest("Incompatible Click version")

        # First, let's see if Click is crashing with a preferred ASCII charset.
        with patch("locale.getpreferredencoding") as gpe:
            gpe.return_value = "ASCII"
            with self.assertRaises(RuntimeError):
                _unicodefun._verify_python_env()
        # Now, let's silence Click...
        grey.patch_click()
        # ...and confirm it's silent.
        with patch("locale.getpreferredencoding") as gpe:
            gpe.return_value = "ASCII"
            try:
                _unicodefun._verify_python_env()
            except RuntimeError as re:
                self.fail(f"`patch_click()` failed, exception still raised: {re}")

    def test_root_logger_not_used_directly(self) -> None:
        def fail(*args: Any, **kwargs: Any) -> None:
            self.fail("Record created with root logger")

        with patch.multiple(
            logging.root,
            debug=fail,
            info=fail,
            warning=fail,
            error=fail,
            critical=fail,
            log=fail,
        ):
            ff(THIS_DIR / "util.py")

    def test_invalid_config_return_code(self) -> None:
        tmp_file = Path(grey.dump_to_file())
        try:
            tmp_config = Path(grey.dump_to_file())
            tmp_config.unlink()
            args = ["--config", str(tmp_config), str(tmp_file)]
            self.invokeBlack(args, exit_code=2, ignore_config=False)
        finally:
            tmp_file.unlink()

    def test_parse_pyproject_toml(self) -> None:
        test_toml_file = THIS_DIR / "test.toml"
        config = grey.parse_pyproject_toml(str(test_toml_file))
        self.assertEqual(config["verbose"], 1)
        self.assertEqual(config["check"], "no")
        self.assertEqual(config["diff"], "y")
        self.assertEqual(config["color"], True)
        self.assertEqual(config["line_length"], 79)
        self.assertEqual(config["target_version"], ["py36", "py37", "py38"])
        self.assertEqual(config["python_cell_magics"], ["custom1", "custom2"])
        self.assertEqual(config["exclude"], r"\.pyi?$")
        self.assertEqual(config["include"], r"\.py?$")

    def test_read_pyproject_toml(self) -> None:
        test_toml_file = THIS_DIR / "test.toml"
        fake_ctx = FakeContext()
        grey.read_pyproject_toml(fake_ctx, FakeParameter(), str(test_toml_file))
        config = fake_ctx.default_map
        self.assertEqual(config["verbose"], "1")
        self.assertEqual(config["check"], "no")
        self.assertEqual(config["diff"], "y")
        self.assertEqual(config["color"], "True")
        self.assertEqual(config["line_length"], "79")
        self.assertEqual(config["target_version"], ["py36", "py37", "py38"])
        self.assertEqual(config["exclude"], r"\.pyi?$")
        self.assertEqual(config["include"], r"\.py?$")

    @pytest.mark.incompatible_with_mypyc
    def test_find_project_root(self) -> None:
        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            test_dir = root / "test"
            test_dir.mkdir()

            src_dir = root / "src"
            src_dir.mkdir()

            root_pyproject = root / "pyproject.toml"
            root_pyproject.touch()
            src_pyproject = src_dir / "pyproject.toml"
            src_pyproject.touch()
            src_python = src_dir / "foo.py"
            src_python.touch()

            self.assertEqual(
                grey.find_project_root((src_dir, test_dir)),
                (root.resolve(), "pyproject.toml"),
            )
            self.assertEqual(
                grey.find_project_root((src_dir,)),
                (src_dir.resolve(), "pyproject.toml"),
            )
            self.assertEqual(
                grey.find_project_root((src_python,)),
                (src_dir.resolve(), "pyproject.toml"),
            )

    @patch(
        "grey.files.find_user_pyproject_toml",
    )
    def test_find_pyproject_toml(self, find_user_pyproject_toml: MagicMock) -> None:
        find_user_pyproject_toml.side_effect = RuntimeError()

        with redirect_stderr(io.StringIO()) as stderr:
            result = grey.files.find_pyproject_toml(
                path_search_start=(str(Path.cwd().root),)
            )

        assert result is None
        err = stderr.getvalue()
        assert "Ignoring user configuration" in err

    @patch(
        "grey.files.find_user_pyproject_toml",
        grey.files.find_user_pyproject_toml.__wrapped__,
    )
    def test_find_user_pyproject_toml_linux(self) -> None:
        if system() == "Windows":
            return

        # Test if XDG_CONFIG_HOME is checked
        with TemporaryDirectory() as workspace:
            tmp_user_config = Path(workspace) / "grey"
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": workspace}):
                self.assertEqual(
                    grey.files.find_user_pyproject_toml(), tmp_user_config.resolve()
                )

        # Test fallback for XDG_CONFIG_HOME
        with patch.dict("os.environ"):
            os.environ.pop("XDG_CONFIG_HOME", None)
            fallback_user_config = Path("~/.config").expanduser() / "grey"
            self.assertEqual(
                grey.files.find_user_pyproject_toml(),
                fallback_user_config.resolve(),
            )

    def test_find_user_pyproject_toml_windows(self) -> None:
        if system() != "Windows":
            return

        user_config_path = Path.home() / ".grey"
        self.assertEqual(
            grey.files.find_user_pyproject_toml(), user_config_path.resolve()
        )

    def test_bpo_33660_workaround(self) -> None:
        if system() == "Windows":
            return

        # https://bugs.python.org/issue33660
        root = Path("/")
        with change_directory(root):
            path = Path("workspace") / "project"
            report = grey.Report(verbose=True)
            normalized_path = grey.normalize_path_maybe_ignore(path, root, report)
            self.assertEqual(normalized_path, "workspace/project")

    def test_normalize_path_ignore_windows_junctions_outside_of_root(self) -> None:
        if system() != "Windows":
            return

        with TemporaryDirectory() as workspace:
            root = Path(workspace)
            junction_dir = root / "junction"
            junction_target_outside_of_root = root / ".."
            os.system(f"mklink /J {junction_dir} {junction_target_outside_of_root}")

            report = grey.Report(verbose=True)
            normalized_path = grey.normalize_path_maybe_ignore(
                junction_dir, root, report
            )
            # Manually delete for Python < 3.8
            os.system(f"rmdir {junction_dir}")

            self.assertEqual(normalized_path, None)

    def test_newline_comment_interaction(self) -> None:
        source = "class A:\\\r\n# type: ignore\n pass\n"
        output = grey.format_str(source, mode=DEFAULT_MODE)
        grey.assert_stable(source, output, mode=DEFAULT_MODE)

    def test_bpo_2142_workaround(self) -> None:

        # https://bugs.python.org/issue2142

        source, _ = read_data("miscellaneous", "missing_final_newline")
        # read_data adds a trailing newline
        source = source.rstrip()
        expected, _ = read_data("miscellaneous", "missing_final_newline.diff")
        tmp_file = Path(grey.dump_to_file(source, ensure_final_newline=False))
        diff_header = re.compile(
            rf"{re.escape(str(tmp_file))}\t\d\d\d\d-\d\d-\d\d "
            r"\d\d:\d\d:\d\d\.\d\d\d\d\d\d \+\d\d\d\d"
        )
        try:
            result = BlackRunner().invoke(grey.main, ["--diff", str(tmp_file)])
            self.assertEqual(result.exit_code, 0)
        finally:
            os.unlink(tmp_file)
        actual = result.output
        actual = diff_header.sub(DETERMINISTIC_HEADER, actual)
        self.assertEqual(actual, expected)

    @staticmethod
    def compare_results(
        result: click.testing.Result, expected_value: str, expected_exit_code: int
    ) -> None:
        """Helper method to test the value and exit code of a click Result."""
        assert (
            result.output == expected_value
        ), "The output did not match the expected value."
        assert result.exit_code == expected_exit_code, "The exit code is incorrect."

    def test_code_option(self) -> None:
        """Test the code option with no changes."""
        code = 'print("Hello world")\n'
        args = ["--code", code]
        result = CliRunner().invoke(grey.main, args)

        self.compare_results(result, code, 0)

    def test_code_option_changed(self) -> None:
        """Test the code option when changes are required."""
        code = "print('hello world')"
        formatted = grey.format_str(code, mode=DEFAULT_MODE)

        args = ["--code", code]
        result = CliRunner().invoke(grey.main, args)

        self.compare_results(result, formatted, 0)

    def test_code_option_check(self) -> None:
        """Test the code option when check is passed."""
        args = ["--check", "--code", 'print("Hello world")\n']
        result = CliRunner().invoke(grey.main, args)
        self.compare_results(result, "", 0)

    def test_code_option_check_changed(self) -> None:
        """Test the code option when changes are required, and check is passed."""
        args = ["--check", "--code", "print('hello world')"]
        result = CliRunner().invoke(grey.main, args)
        self.compare_results(result, "", 1)

    def test_code_option_diff(self) -> None:
        """Test the code option when diff is passed."""
        code = "print('hello world')"
        formatted = grey.format_str(code, mode=DEFAULT_MODE)
        result_diff = diff(code, formatted, "STDIN", "STDOUT")

        args = ["--diff", "--code", code]
        result = CliRunner().invoke(grey.main, args)

        # Remove time from diff
        output = DIFF_TIME.sub("", result.output)

        assert output == result_diff, "The output did not match the expected value."
        assert result.exit_code == 0, "The exit code is incorrect."

    def test_code_option_color_diff(self) -> None:
        """Test the code option when color and diff are passed."""
        code = "print('hello world')"
        formatted = grey.format_str(code, mode=DEFAULT_MODE)

        result_diff = diff(code, formatted, "STDIN", "STDOUT")
        result_diff = color_diff(result_diff)

        args = ["--diff", "--color", "--code", code]
        result = CliRunner().invoke(grey.main, args)

        # Remove time from diff
        output = DIFF_TIME.sub("", result.output)

        assert output == result_diff, "The output did not match the expected value."
        assert result.exit_code == 0, "The exit code is incorrect."

    @pytest.mark.incompatible_with_mypyc
    def test_code_option_safe(self) -> None:
        """Test that the code option throws an error when the sanity checks fail."""
        # Patch grey.assert_equivalent to ensure the sanity checks fail
        with patch.object(grey, "assert_equivalent", side_effect=AssertionError):
            code = 'print("Hello world")'
            error_msg = f"{code}\nerror: cannot format <string>: \n"

            args = ["--safe", "--code", code]
            result = CliRunner().invoke(grey.main, args)

            self.compare_results(result, error_msg, 123)

    def test_code_option_fast(self) -> None:
        """Test that the code option ignores errors when the sanity checks fail."""
        # Patch grey.assert_equivalent to ensure the sanity checks fail
        with patch.object(grey, "assert_equivalent", side_effect=AssertionError):
            code = 'print("Hello world")'
            formatted = grey.format_str(code, mode=DEFAULT_MODE)

            args = ["--fast", "--code", code]
            result = CliRunner().invoke(grey.main, args)

            self.compare_results(result, formatted, 0)

    @pytest.mark.incompatible_with_mypyc
    def test_code_option_config(self) -> None:
        """
        Test that the code option finds the pyproject.toml in the current directory.
        """
        with patch.object(grey, "parse_pyproject_toml", return_value={}) as parse:
            args = ["--code", "print"]
            # This is the only directory known to contain a pyproject.toml
            with change_directory(PROJECT_ROOT):
                CliRunner().invoke(grey.main, args)
                pyproject_path = Path(Path.cwd(), "pyproject.toml").resolve()

            assert (
                len(parse.mock_calls) >= 1
            ), "Expected config parse to be called with the current directory."

            _, call_args, _ = parse.mock_calls[0]
            assert (
                call_args[0].lower() == str(pyproject_path).lower()
            ), "Incorrect config loaded."

    @pytest.mark.incompatible_with_mypyc
    def test_code_option_parent_config(self) -> None:
        """
        Test that the code option finds the pyproject.toml in the parent directory.
        """
        with patch.object(grey, "parse_pyproject_toml", return_value={}) as parse:
            with change_directory(THIS_DIR):
                args = ["--code", "print"]
                CliRunner().invoke(grey.main, args)

                pyproject_path = Path(Path().cwd().parent, "pyproject.toml").resolve()
                assert (
                    len(parse.mock_calls) >= 1
                ), "Expected config parse to be called with the current directory."

                _, call_args, _ = parse.mock_calls[0]
                assert (
                    call_args[0].lower() == str(pyproject_path).lower()
                ), "Incorrect config loaded."

    def test_for_handled_unexpected_eof_error(self) -> None:
        """
        Test that an unexpected EOF SyntaxError is nicely presented.
        """
        with pytest.raises(grey.parsing.InvalidInput) as exc_info:
            grey.lib2to3_parse("print(", {})

        exc_info.match("Cannot parse: 2:0: EOF in multi-line statement")

    def test_equivalency_ast_parse_failure_includes_error(self) -> None:
        with pytest.raises(AssertionError) as err:
            grey.assert_equivalent("a«»a  = 1", "a«»a  = 1")

        err.match("--safe")
        # Unfortunately the SyntaxError message has changed in newer versions so we
        # can't match it directly.
        err.match("invalid character")
        err.match(r"\(<unknown>, line 1\)")


class TestCaching:
    def test_get_cache_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Create multiple cache directories
        workspace1 = tmp_path / "ws1"
        workspace1.mkdir()
        workspace2 = tmp_path / "ws2"
        workspace2.mkdir()

        # Force user_cache_dir to use the temporary directory for easier assertions
        patch_user_cache_dir = patch(
            target="grey.cache.user_cache_dir",
            autospec=True,
            return_value=str(workspace1),
        )

        # If BLACK_CACHE_DIR is not set, use user_cache_dir
        monkeypatch.delenv("BLACK_CACHE_DIR", raising=False)
        with patch_user_cache_dir:
            assert get_cache_dir() == workspace1

        # If it is set, use the path provided in the env var.
        monkeypatch.setenv("BLACK_CACHE_DIR", str(workspace2))
        assert get_cache_dir() == workspace2

    def test_cache_broken_file(self) -> None:
        mode = DEFAULT_MODE
        with cache_dir() as workspace:
            cache_file = get_cache_file(mode)
            cache_file.write_text("this is not a pickle")
            assert grey.read_cache(mode) == {}
            src = (workspace / "test.py").resolve()
            src.write_text("print('hello')")
            invokeBlack([str(src)])
            cache = grey.read_cache(mode)
            assert str(src) in cache

    def test_cache_single_file_already_cached(self) -> None:
        mode = DEFAULT_MODE
        with cache_dir() as workspace:
            src = (workspace / "test.py").resolve()
            src.write_text("print('hello')")
            grey.write_cache({}, [src], mode)
            invokeBlack([str(src)])
            assert src.read_text() == "print('hello')"

    @event_loop()
    def test_cache_multiple_files(self) -> None:
        mode = DEFAULT_MODE
        with cache_dir() as workspace, patch(
            "concurrent.futures.ProcessPoolExecutor", new=ThreadPoolExecutor
        ):
            one = (workspace / "one.py").resolve()
            with one.open("w") as fobj:
                fobj.write("print('hello')")
            two = (workspace / "two.py").resolve()
            with two.open("w") as fobj:
                fobj.write("print('hello')")
            grey.write_cache({}, [one], mode)
            invokeBlack([str(workspace)])
            with one.open("r") as fobj:
                assert fobj.read() == "print('hello')"
            with two.open("r") as fobj:
                assert fobj.read() == 'print("hello")\n'
            cache = grey.read_cache(mode)
            assert str(one) in cache
            assert str(two) in cache

    @pytest.mark.parametrize("color", [False, True], ids=["no-color", "with-color"])
    def test_no_cache_when_writeback_diff(self, color: bool) -> None:
        mode = DEFAULT_MODE
        with cache_dir() as workspace:
            src = (workspace / "test.py").resolve()
            with src.open("w") as fobj:
                fobj.write("print('hello')")
            with patch("grey.read_cache") as read_cache, patch(
                "grey.write_cache"
            ) as write_cache:
                cmd = [str(src), "--diff"]
                if color:
                    cmd.append("--color")
                invokeBlack(cmd)
                cache_file = get_cache_file(mode)
                assert cache_file.exists() is False
                write_cache.assert_not_called()
                read_cache.assert_not_called()

    @pytest.mark.parametrize("color", [False, True], ids=["no-color", "with-color"])
    @event_loop()
    def test_output_locking_when_writeback_diff(self, color: bool) -> None:
        with cache_dir() as workspace:
            for tag in range(0, 4):
                src = (workspace / f"test{tag}.py").resolve()
                with src.open("w") as fobj:
                    fobj.write("print('hello')")
            with patch("grey.Manager", wraps=multiprocessing.Manager) as mgr:
                cmd = ["--diff", str(workspace)]
                if color:
                    cmd.append("--color")
                invokeBlack(cmd, exit_code=0)
                # this isn't quite doing what we want, but if it _isn't_
                # called then we cannot be using the lock it provides
                mgr.assert_called()

    def test_no_cache_when_stdin(self) -> None:
        mode = DEFAULT_MODE
        with cache_dir():
            result = CliRunner().invoke(
                grey.main, ["-"], input=BytesIO(b"print('hello')")
            )
            assert not result.exit_code
            cache_file = get_cache_file(mode)
            assert not cache_file.exists()

    def test_read_cache_no_cachefile(self) -> None:
        mode = DEFAULT_MODE
        with cache_dir():
            assert grey.read_cache(mode) == {}

    def test_write_cache_read_cache(self) -> None:
        mode = DEFAULT_MODE
        with cache_dir() as workspace:
            src = (workspace / "test.py").resolve()
            src.touch()
            grey.write_cache({}, [src], mode)
            cache = grey.read_cache(mode)
            assert str(src) in cache
            assert cache[str(src)] == grey.get_cache_info(src)

    def test_filter_cached(self) -> None:
        with TemporaryDirectory() as workspace:
            path = Path(workspace)
            uncached = (path / "uncached").resolve()
            cached = (path / "cached").resolve()
            cached_but_changed = (path / "changed").resolve()
            uncached.touch()
            cached.touch()
            cached_but_changed.touch()
            cache = {
                str(cached): grey.get_cache_info(cached),
                str(cached_but_changed): (0.0, 0),
            }
            todo, done = grey.filter_cached(
                cache, {uncached, cached, cached_but_changed}
            )
            assert todo == {uncached, cached_but_changed}
            assert done == {cached}

    def test_write_cache_creates_directory_if_needed(self) -> None:
        mode = DEFAULT_MODE
        with cache_dir(exists=False) as workspace:
            assert not workspace.exists()
            grey.write_cache({}, [], mode)
            assert workspace.exists()

    @event_loop()
    def test_failed_formatting_does_not_get_cached(self) -> None:
        mode = DEFAULT_MODE
        with cache_dir() as workspace, patch(
            "concurrent.futures.ProcessPoolExecutor", new=ThreadPoolExecutor
        ):
            failing = (workspace / "failing.py").resolve()
            with failing.open("w") as fobj:
                fobj.write("not actually python")
            clean = (workspace / "clean.py").resolve()
            with clean.open("w") as fobj:
                fobj.write('print("hello")\n')
            invokeBlack([str(workspace)], exit_code=123)
            cache = grey.read_cache(mode)
            assert str(failing) not in cache
            assert str(clean) in cache

    def test_write_cache_write_fail(self) -> None:
        mode = DEFAULT_MODE
        with cache_dir(), patch.object(Path, "open") as mock:
            mock.side_effect = OSError
            grey.write_cache({}, [], mode)

    def test_read_cache_line_lengths(self) -> None:
        mode = DEFAULT_MODE
        short_mode = replace(DEFAULT_MODE, line_length=1)
        with cache_dir() as workspace:
            path = (workspace / "file.py").resolve()
            path.touch()
            grey.write_cache({}, [path], mode)
            one = grey.read_cache(mode)
            assert str(path) in one
            two = grey.read_cache(short_mode)
            assert str(path) not in two


def assert_collected_sources(
    src: Sequence[Union[str, Path]],
    expected: Sequence[Union[str, Path]],
    *,
    ctx: Optional[FakeContext] = None,
    exclude: Optional[str] = None,
    include: Optional[str] = None,
    extend_exclude: Optional[str] = None,
    force_exclude: Optional[str] = None,
    stdin_filename: Optional[str] = None,
) -> None:
    gs_src = tuple(str(Path(s)) for s in src)
    gs_expected = [Path(s) for s in expected]
    gs_exclude = None if exclude is None else compile_pattern(exclude)
    gs_include = DEFAULT_INCLUDE if include is None else compile_pattern(include)
    gs_extend_exclude = (
        None if extend_exclude is None else compile_pattern(extend_exclude)
    )
    gs_force_exclude = None if force_exclude is None else compile_pattern(force_exclude)
    collected = grey.get_sources(
        ctx=ctx or FakeContext(),
        src=gs_src,
        quiet=False,
        verbose=False,
        include=gs_include,
        exclude=gs_exclude,
        extend_exclude=gs_extend_exclude,
        force_exclude=gs_force_exclude,
        report=grey.Report(),
        stdin_filename=stdin_filename,
    )
    assert sorted(collected) == sorted(gs_expected)


class TestFileCollection:
    def test_include_exclude(self) -> None:
        path = THIS_DIR / "data" / "include_exclude_tests"
        src = [path]
        expected = [
            Path(path / "b/dont_exclude/a.py"),
            Path(path / "b/dont_exclude/a.pyi"),
        ]
        assert_collected_sources(
            src,
            expected,
            include=r"\.pyi?$",
            exclude=r"/exclude/|/\.definitely_exclude/",
        )

    def test_gitignore_used_as_default(self) -> None:
        base = Path(DATA_DIR / "include_exclude_tests")
        expected = [
            base / "b/.definitely_exclude/a.py",
            base / "b/.definitely_exclude/a.pyi",
        ]
        src = [base / "b/"]
        ctx = FakeContext()
        ctx.obj["root"] = base
        assert_collected_sources(src, expected, ctx=ctx, extend_exclude=r"/exclude/")

    @patch("grey.find_project_root", lambda *args: (THIS_DIR.resolve(), None))
    def test_exclude_for_issue_1572(self) -> None:
        # Exclude shouldn't touch files that were explicitly given to Black through the
        # CLI. Exclude is supposed to only apply to the recursive discovery of files.
        # https://github.com/psf/grey/issues/1572
        path = DATA_DIR / "include_exclude_tests"
        src = [path / "b/exclude/a.py"]
        expected = [path / "b/exclude/a.py"]
        assert_collected_sources(src, expected, include="", exclude=r"/exclude/|a\.py")

    def test_gitignore_exclude(self) -> None:
        path = THIS_DIR / "data" / "include_exclude_tests"
        include = re.compile(r"\.pyi?$")
        exclude = re.compile(r"")
        report = grey.Report()
        gitignore = PathSpec.from_lines(
            "gitwildmatch", ["exclude/", ".definitely_exclude"]
        )
        sources: List[Path] = []
        expected = [
            Path(path / "b/dont_exclude/a.py"),
            Path(path / "b/dont_exclude/a.pyi"),
        ]
        this_abs = THIS_DIR.resolve()
        sources.extend(
            grey.gen_python_files(
                path.iterdir(),
                this_abs,
                include,
                exclude,
                None,
                None,
                report,
                gitignore,
                verbose=False,
                quiet=False,
            )
        )
        assert sorted(expected) == sorted(sources)

    def test_nested_gitignore(self) -> None:
        path = Path(THIS_DIR / "data" / "nested_gitignore_tests")
        include = re.compile(r"\.pyi?$")
        exclude = re.compile(r"")
        root_gitignore = grey.files.get_gitignore(path)
        report = grey.Report()
        expected: List[Path] = [
            Path(path / "x.py"),
            Path(path / "root/b.py"),
            Path(path / "root/c.py"),
            Path(path / "root/child/c.py"),
        ]
        this_abs = THIS_DIR.resolve()
        sources = list(
            grey.gen_python_files(
                path.iterdir(),
                this_abs,
                include,
                exclude,
                None,
                None,
                report,
                root_gitignore,
                verbose=False,
                quiet=False,
            )
        )
        assert sorted(expected) == sorted(sources)

    def test_invalid_gitignore(self) -> None:
        path = THIS_DIR / "data" / "invalid_gitignore_tests"
        empty_config = path / "pyproject.toml"
        result = BlackRunner().invoke(
            grey.main, ["--verbose", "--config", str(empty_config), str(path)]
        )
        assert result.exit_code == 1
        assert result.stderr_bytes is not None

        gitignore = path / ".gitignore"
        assert f"Could not parse {gitignore}" in result.stderr_bytes.decode()

    def test_invalid_nested_gitignore(self) -> None:
        path = THIS_DIR / "data" / "invalid_nested_gitignore_tests"
        empty_config = path / "pyproject.toml"
        result = BlackRunner().invoke(
            grey.main, ["--verbose", "--config", str(empty_config), str(path)]
        )
        assert result.exit_code == 1
        assert result.stderr_bytes is not None

        gitignore = path / "a" / ".gitignore"
        assert f"Could not parse {gitignore}" in result.stderr_bytes.decode()

    def test_empty_include(self) -> None:
        path = DATA_DIR / "include_exclude_tests"
        src = [path]
        expected = [
            Path(path / "b/exclude/a.pie"),
            Path(path / "b/exclude/a.py"),
            Path(path / "b/exclude/a.pyi"),
            Path(path / "b/dont_exclude/a.pie"),
            Path(path / "b/dont_exclude/a.py"),
            Path(path / "b/dont_exclude/a.pyi"),
            Path(path / "b/.definitely_exclude/a.pie"),
            Path(path / "b/.definitely_exclude/a.py"),
            Path(path / "b/.definitely_exclude/a.pyi"),
            Path(path / ".gitignore"),
            Path(path / "pyproject.toml"),
        ]
        # Setting exclude explicitly to an empty string to block .gitignore usage.
        assert_collected_sources(src, expected, include="", exclude="")

    def test_extend_exclude(self) -> None:
        path = DATA_DIR / "include_exclude_tests"
        src = [path]
        expected = [
            Path(path / "b/exclude/a.py"),
            Path(path / "b/dont_exclude/a.py"),
        ]
        assert_collected_sources(
            src, expected, exclude=r"\.pyi$", extend_exclude=r"\.definitely_exclude"
        )

    @pytest.mark.incompatible_with_mypyc
    def test_symlink_out_of_root_directory(self) -> None:
        path = MagicMock()
        root = THIS_DIR.resolve()
        child = MagicMock()
        include = re.compile(grey.DEFAULT_INCLUDES)
        exclude = re.compile(grey.DEFAULT_EXCLUDES)
        report = grey.Report()
        gitignore = PathSpec.from_lines("gitwildmatch", [])
        # `child` should behave like a symlink which resolved path is clearly
        # outside of the `root` directory.
        path.iterdir.return_value = [child]
        child.resolve.return_value = Path("/a/b/c")
        child.as_posix.return_value = "/a/b/c"
        try:
            list(
                grey.gen_python_files(
                    path.iterdir(),
                    root,
                    include,
                    exclude,
                    None,
                    None,
                    report,
                    gitignore,
                    verbose=False,
                    quiet=False,
                )
            )
        except ValueError as ve:
            pytest.fail(f"`get_python_files_in_dir()` failed: {ve}")
        path.iterdir.assert_called_once()
        child.resolve.assert_called_once()

    @patch("grey.find_project_root", lambda *args: (THIS_DIR.resolve(), None))
    def test_get_sources_with_stdin(self) -> None:
        src = ["-"]
        expected = ["-"]
        assert_collected_sources(src, expected, include="", exclude=r"/exclude/|a\.py")

    @patch("grey.find_project_root", lambda *args: (THIS_DIR.resolve(), None))
    def test_get_sources_with_stdin_filename(self) -> None:
        src = ["-"]
        stdin_filename = str(THIS_DIR / "data/collections.py")
        expected = [f"__BLACK_STDIN_FILENAME__{stdin_filename}"]
        assert_collected_sources(
            src,
            expected,
            exclude=r"/exclude/a\.py",
            stdin_filename=stdin_filename,
        )

    @patch("grey.find_project_root", lambda *args: (THIS_DIR.resolve(), None))
    def test_get_sources_with_stdin_filename_and_exclude(self) -> None:
        # Exclude shouldn't exclude stdin_filename since it is mimicking the
        # file being passed directly. This is the same as
        # test_exclude_for_issue_1572
        path = DATA_DIR / "include_exclude_tests"
        src = ["-"]
        stdin_filename = str(path / "b/exclude/a.py")
        expected = [f"__BLACK_STDIN_FILENAME__{stdin_filename}"]
        assert_collected_sources(
            src,
            expected,
            exclude=r"/exclude/|a\.py",
            stdin_filename=stdin_filename,
        )

    @patch("grey.find_project_root", lambda *args: (THIS_DIR.resolve(), None))
    def test_get_sources_with_stdin_filename_and_extend_exclude(self) -> None:
        # Extend exclude shouldn't exclude stdin_filename since it is mimicking the
        # file being passed directly. This is the same as
        # test_exclude_for_issue_1572
        src = ["-"]
        path = THIS_DIR / "data" / "include_exclude_tests"
        stdin_filename = str(path / "b/exclude/a.py")
        expected = [f"__BLACK_STDIN_FILENAME__{stdin_filename}"]
        assert_collected_sources(
            src,
            expected,
            extend_exclude=r"/exclude/|a\.py",
            stdin_filename=stdin_filename,
        )

    @patch("grey.find_project_root", lambda *args: (THIS_DIR.resolve(), None))
    def test_get_sources_with_stdin_filename_and_force_exclude(self) -> None:
        # Force exclude should exclude the file when passing it through
        # stdin_filename
        path = THIS_DIR / "data" / "include_exclude_tests"
        stdin_filename = str(path / "b/exclude/a.py")
        assert_collected_sources(
            src=["-"],
            expected=[],
            force_exclude=r"/exclude/|a\.py",
            stdin_filename=stdin_filename,
        )


try:
    with open(grey.__file__, "r", encoding="utf-8") as _bf:
        grey_source_lines = _bf.readlines()
except UnicodeDecodeError:
    if not grey.COMPILED:
        raise


def tracefunc(
    frame: types.FrameType, event: str, arg: Any
) -> Callable[[types.FrameType, str, Any], Any]:
    """Show function calls `from grey/__init__.py` as they happen.

    Register this with `sys.settrace()` in a test you're debugging.
    """
    if event != "call":
        return tracefunc

    stack = len(inspect.stack()) - 19
    stack *= 2
    filename = frame.f_code.co_filename
    lineno = frame.f_lineno
    func_sig_lineno = lineno - 1
    funcname = grey_source_lines[func_sig_lineno].strip()
    while funcname.startswith("@"):
        func_sig_lineno += 1
        funcname = grey_source_lines[func_sig_lineno].strip()
    if "grey/__init__.py" in filename:
        print(f"{' ' * stack}{lineno}:{funcname}")
    return tracefunc