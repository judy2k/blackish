from dataclasses import replace
from typing import Any, Iterator, List
from unittest.mock import patch

import pytest

import blackish
from tests.util import (
    DEFAULT_MODE,
    PY36_VERSIONS,
    assert_format,
    dump_to_stderr,
    read_data,
    all_data_cases,
)

SOURCES: List[str] = [
    "src/blackish/__init__.py",
    "src/blackish/__main__.py",
    "src/blackish/brackets.py",
    "src/blackish/cache.py",
    "src/blackish/comments.py",
    "src/blackish/concurrency.py",
    "src/blackish/const.py",
    "src/blackish/debug.py",
    "src/blackish/files.py",
    "src/blackish/linegen.py",
    "src/blackish/lines.py",
    "src/blackish/mode.py",
    "src/blackish/nodes.py",
    "src/blackish/numerics.py",
    "src/blackish/output.py",
    "src/blackish/parsing.py",
    "src/blackish/report.py",
    "src/blackish/rusty.py",
    "src/blackish/strings.py",
    "src/blackish/trans.py",
    "src/blackishd/__init__.py",
    "src/blib2to3/pygram.py",
    "src/blib2to3/pytree.py",
    "src/blib2to3/pgen2/conv.py",
    "src/blib2to3/pgen2/driver.py",
    "src/blib2to3/pgen2/grammar.py",
    "src/blib2to3/pgen2/literals.py",
    "src/blib2to3/pgen2/parse.py",
    "src/blib2to3/pgen2/pgen.py",
    "src/blib2to3/pgen2/tokenize.py",
    "src/blib2to3/pgen2/token.py",
    "setup.py",
    "tests/test_blackish.py",
    "tests/test_blackishd.py",
    "tests/test_format.py",
    "tests/optional.py",
    "tests/util.py",
    "tests/conftest.py",
]


@pytest.fixture(autouse=True)
def patch_dump_to_file(request: Any) -> Iterator[None]:
    with patch("blackish.dump_to_file", dump_to_stderr):
        yield


def check_file(
    subdir: str, filename: str, mode: blackish.Mode, *, data: bool = True
) -> None:
    source, expected = read_data(subdir, filename, data=data)
    assert_format(source, expected, mode, fast=False)


@pytest.mark.parametrize("filename", all_data_cases("simple_cases"))
def test_simple_format(filename: str) -> None:
    check_file("simple_cases", filename, DEFAULT_MODE)


@pytest.mark.parametrize("filename", all_data_cases("preview"))
def test_preview_format(filename: str) -> None:
    check_file("preview", filename, blackish.Mode(preview=True))


@pytest.mark.parametrize("filename", all_data_cases("preview_39"))
def test_preview_minimum_python_39_format(filename: str) -> None:
    source, expected = read_data("preview_39", filename)
    mode = blackish.Mode(preview=True)
    assert_format(source, expected, mode, minimum_version=(3, 9))


@pytest.mark.parametrize("filename", SOURCES)
def test_source_is_formatted(filename: str) -> None:
    check_file("", filename, DEFAULT_MODE, data=False)


# =============== #
# Complex cases
# ============= #


def test_empty() -> None:
    source = expected = ""
    assert_format(source, expected)


@pytest.mark.parametrize("filename", all_data_cases("py_36"))
def test_python_36(filename: str) -> None:
    source, expected = read_data("py_36", filename)
    mode = blackish.Mode(target_versions=PY36_VERSIONS)
    assert_format(source, expected, mode, minimum_version=(3, 6))


@pytest.mark.parametrize("filename", all_data_cases("py_37"))
def test_python_37(filename: str) -> None:
    source, expected = read_data("py_37", filename)
    mode = blackish.Mode(target_versions={blackish.TargetVersion.PY37})
    assert_format(source, expected, mode, minimum_version=(3, 7))


@pytest.mark.parametrize("filename", all_data_cases("py_38"))
def test_python_38(filename: str) -> None:
    source, expected = read_data("py_38", filename)
    mode = blackish.Mode(target_versions={blackish.TargetVersion.PY38})
    assert_format(source, expected, mode, minimum_version=(3, 8))


@pytest.mark.parametrize("filename", all_data_cases("py_39"))
def test_python_39(filename: str) -> None:
    source, expected = read_data("py_39", filename)
    mode = blackish.Mode(target_versions={blackish.TargetVersion.PY39})
    assert_format(source, expected, mode, minimum_version=(3, 9))


@pytest.mark.parametrize("filename", all_data_cases("py_310"))
def test_python_310(filename: str) -> None:
    source, expected = read_data("py_310", filename)
    mode = blackish.Mode(target_versions={blackish.TargetVersion.PY310})
    assert_format(source, expected, mode, minimum_version=(3, 10))


@pytest.mark.parametrize("filename", all_data_cases("py_310"))
def test_python_310_without_target_version(filename: str) -> None:
    source, expected = read_data("py_310", filename)
    mode = blackish.Mode()
    assert_format(source, expected, mode, minimum_version=(3, 10))


def test_patma_invalid() -> None:
    source, expected = read_data("miscellaneous", "pattern_matching_invalid")
    mode = blackish.Mode(target_versions={blackish.TargetVersion.PY310})
    with pytest.raises(blackish.parsing.InvalidInput) as exc_info:
        assert_format(source, expected, mode, minimum_version=(3, 10))

    exc_info.match("Cannot parse: 10:11")


@pytest.mark.parametrize("filename", all_data_cases("py_311"))
def test_python_311(filename: str) -> None:
    source, expected = read_data("py_311", filename)
    mode = blackish.Mode(target_versions={blackish.TargetVersion.PY311})
    assert_format(source, expected, mode, minimum_version=(3, 11))


@pytest.mark.parametrize("filename", all_data_cases("fast"))
def test_fast_cases(filename: str) -> None:
    source, expected = read_data("fast", filename)
    assert_format(source, expected, fast=True)


def test_python_2_hint() -> None:
    with pytest.raises(blackish.parsing.InvalidInput) as exc_info:
        assert_format("print 'daylily'", "print 'daylily'")
    exc_info.match(blackish.parsing.PY2_HINT)


def test_docstring_no_string_normalization() -> None:
    """Like test_docstring but with string normalization off."""
    source, expected = read_data("miscellaneous", "docstring_no_string_normalization")
    mode = replace(DEFAULT_MODE, string_normalization=False)
    assert_format(source, expected, mode)


def test_long_strings_flag_disabled() -> None:
    """Tests for turning off the string processing logic."""
    source, expected = read_data("miscellaneous", "long_strings_flag_disabled")
    mode = replace(DEFAULT_MODE, experimental_string_processing=False)
    assert_format(source, expected, mode)


def test_stub() -> None:
    mode = replace(DEFAULT_MODE, is_pyi=True)
    source, expected = read_data("miscellaneous", "stub.pyi")
    assert_format(source, expected, mode)


def test_power_op_newline() -> None:
    # requires line_length=0
    source, expected = read_data("miscellaneous", "power_op_newline")
    assert_format(source, expected, mode=blackish.Mode(line_length=0))
