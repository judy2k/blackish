import re
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner
import pytest

from tests.util import read_data, DETERMINISTIC_HEADER

try:
    import blackishd
    from aiohttp.test_utils import AioHTTPTestCase
    from aiohttp import web
except ImportError as e:
    raise RuntimeError("Please install Black with the 'd' extra") from e

try:
    from aiohttp.test_utils import unittest_run_loop
except ImportError:
    # unittest_run_loop is unnecessary and a no-op since aiohttp 3.8, and aiohttp 4
    # removed it. To maintain compatibility we can make our own no-op decorator.
    def unittest_run_loop(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func


@pytest.mark.blackishd
class BlackDTestCase(AioHTTPTestCase):
    def test_blackishd_main(self) -> None:
        with patch("blackishd.web.run_app"):
            result = CliRunner().invoke(blackishd.main, [])
            if result.exception is not None:
                raise result.exception
            self.assertEqual(result.exit_code, 0)

    async def get_application(self) -> web.Application:
        return blackishd.make_app()

    @unittest_run_loop
    async def test_blackishd_request_needs_formatting(self) -> None:
        response = await self.client.post("/", data=b"print('hello world')")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.charset, "utf8")
        self.assertEqual(await response.read(), b'print("hello world")\n')

    @unittest_run_loop
    async def test_blackishd_request_no_change(self) -> None:
        response = await self.client.post("/", data=b'print("hello world")\n')
        self.assertEqual(response.status, 204)
        self.assertEqual(await response.read(), b"")

    @unittest_run_loop
    async def test_blackishd_request_syntax_error(self) -> None:
        response = await self.client.post("/", data=b"what even ( is")
        self.assertEqual(response.status, 400)
        content = await response.text()
        self.assertTrue(
            content.startswith("Cannot parse"),
            msg=f"Expected error to start with 'Cannot parse', got {repr(content)}",
        )

    @unittest_run_loop
    async def test_blackishd_unsupported_version(self) -> None:
        response = await self.client.post(
            "/", data=b"what", headers={blackishd.PROTOCOL_VERSION_HEADER: "2"}
        )
        self.assertEqual(response.status, 501)

    @unittest_run_loop
    async def test_blackishd_supported_version(self) -> None:
        response = await self.client.post(
            "/", data=b"what", headers={blackishd.PROTOCOL_VERSION_HEADER: "1"}
        )
        self.assertEqual(response.status, 200)

    @unittest_run_loop
    async def test_blackishd_invalid_python_variant(self) -> None:
        async def check(header_value: str, expected_status: int = 400) -> None:
            response = await self.client.post(
                "/",
                data=b"what",
                headers={blackishd.PYTHON_VARIANT_HEADER: header_value},
            )
            self.assertEqual(response.status, expected_status)

        await check("lol")
        await check("ruby3.5")
        await check("pyi3.6")
        await check("py1.5")
        await check("2")
        await check("2.7")
        await check("py2.7")
        await check("2.8")
        await check("py2.8")
        await check("3.0")
        await check("pypy3.0")
        await check("jython3.4")

    @unittest_run_loop
    async def test_blackishd_pyi(self) -> None:
        source, expected = read_data("miscellaneous", "stub.pyi")
        response = await self.client.post(
            "/", data=source, headers={blackishd.PYTHON_VARIANT_HEADER: "pyi"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), expected)

    @unittest_run_loop
    async def test_blackishd_diff(self) -> None:
        diff_header = re.compile(
            r"(In|Out)\t\d\d\d\d-\d\d-\d\d \d\d:\d\d:\d\d\.\d\d\d\d\d\d \+\d\d\d\d"
        )

        source, _ = read_data("miscellaneous", "blackishd_diff")
        expected, _ = read_data("miscellaneous", "blackishd_diff.diff")

        response = await self.client.post(
            "/", data=source, headers={blackishd.DIFF_HEADER: "true"}
        )
        self.assertEqual(response.status, 200)

        actual = await response.text()
        actual = diff_header.sub(DETERMINISTIC_HEADER, actual)
        self.assertEqual(actual, expected)

    @unittest_run_loop
    async def test_blackishd_python_variant(self) -> None:
        code = (
            "def f(\n"
            "    and_has_a_bunch_of,\n"
            "    very_long_arguments_too,\n"
            "    and_lots_of_them_as_well_lol,\n"
            "    **and_very_long_keyword_arguments\n"
            "):\n"
            "    pass\n"
        )

        async def check(header_value: str, expected_status: int) -> None:
            response = await self.client.post(
                "/", data=code, headers={blackishd.PYTHON_VARIANT_HEADER: header_value}
            )
            self.assertEqual(
                response.status, expected_status, msg=await response.text()
            )

        await check("3.6", 200)
        await check("py3.6", 200)
        await check("3.6,3.7", 200)
        await check("3.6,py3.7", 200)
        await check("py36,py37", 200)
        await check("36", 200)
        await check("3.6.4", 200)
        await check("3.4", 204)
        await check("py3.4", 204)
        await check("py34,py36", 204)
        await check("34", 204)

    @unittest_run_loop
    async def test_blackishd_line_length(self) -> None:
        response = await self.client.post(
            "/", data=b'print("hello")\n', headers={blackishd.LINE_LENGTH_HEADER: "7"}
        )
        self.assertEqual(response.status, 200)

    @unittest_run_loop
    async def test_blackishd_invalid_line_length(self) -> None:
        response = await self.client.post(
            "/", data=b'print("hello")\n', headers={blackishd.LINE_LENGTH_HEADER: "NaN"}
        )
        self.assertEqual(response.status, 400)

    @unittest_run_loop
    async def test_blackishd_response_black_version_header(self) -> None:
        response = await self.client.post("/")
        self.assertIsNotNone(response.headers.get(blackishd.BLACK_VERSION_HEADER))

    @unittest_run_loop
    async def test_cors_preflight(self) -> None:
        response = await self.client.options(
            "/",
            headers={
                "Access-Control-Request-Method": "POST",
                "Origin": "*",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(response.headers.get("Access-Control-Allow-Origin"))
        self.assertIsNotNone(response.headers.get("Access-Control-Allow-Headers"))
        self.assertIsNotNone(response.headers.get("Access-Control-Allow-Methods"))

    @unittest_run_loop
    async def test_cors_headers_present(self) -> None:
        response = await self.client.post("/", headers={"Origin": "*"})
        self.assertIsNotNone(response.headers.get("Access-Control-Allow-Origin"))
        self.assertIsNotNone(response.headers.get("Access-Control-Expose-Headers"))
