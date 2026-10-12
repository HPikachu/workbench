from __future__ import annotations

import contextlib
import importlib
import io
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

from requests.cookies import create_cookie
import yt_dlp
from yt_dlp.utils import sanitize_filename


class _Route:
    def __init__(
        self,
        body: bytes = b"",
        *,
        content_type: str = "application/octet-stream",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self.content_type = content_type
        self.status = status
        self.headers = headers or {}


class _FixtureRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve(write_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._serve(write_body=False)

    def _serve(self, *, write_body: bool) -> None:
        fixture_server: _FixtureServer = self.server.fixture_server  # type: ignore[attr-defined]
        request_path = urlsplit(self.path).path
        fixture_server.requests.append(
            {
                "path": request_path,
                "query": urlsplit(self.path).query,
                "headers": {key: value for key, value in self.headers.items()},
            }
        )

        route = fixture_server.routes.get(request_path)
        if route is None:
            self.send_error(404, "fixture route not found")
            return

        body = route.body
        response_status = route.status
        response_headers = dict(route.headers)
        range_header = self.headers.get("Range")
        if response_status == 200 and range_header and range_header.startswith("bytes="):
            start_end = range_header.removeprefix("bytes=").split(",", 1)[0]
            start_text, _, end_text = start_end.partition("-")
            try:
                start = int(start_text)
                end = int(end_text) if end_text else len(body) - 1
            except ValueError:
                start = 0
                end = len(body) - 1
            if 0 <= start <= end < len(body):
                body = body[start : end + 1]
                response_status = 206
                response_headers["Content-Range"] = (
                    f"bytes {start}-{end}/{len(route.body)}"
                )

        self.send_response(response_status)
        self.send_header("Content-Type", route.content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.end_headers()
        if write_body:
            self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _FixtureServer:
    def __init__(self) -> None:
        self.routes: dict[str, _Route] = {}
        self.requests: list[dict[str, Any]] = []
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self, ssl_context: ssl.SSLContext | None = None) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureRequestHandler)
        self.httpd.fixture_server = self  # type: ignore[attr-defined]
        if ssl_context is not None:
            self.httpd.socket = ssl_context.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        assert self.httpd is not None
        host, port = self.httpd.server_address[:2]
        scheme = "https" if isinstance(self.httpd.socket, ssl.SSLSocket) else "http"
        return f"{scheme}://{host}:{port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def add_bytes(
        self,
        path: str,
        body: bytes,
        *,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.routes[path] = _Route(
            body,
            content_type=content_type,
            headers=headers,
        )

    def add_redirect(self, path: str, location: str) -> None:
        self.routes[path] = _Route(
            content_type="text/plain",
            status=302,
            headers={"Location": location},
        )


class _StaticClient:
    def __init__(self, title: str, media_url: str) -> None:
        self.title = title
        self.media_url = media_url
        self.calls: list[str] = []

    def get_video_info(self, url: str) -> tuple[str, str]:
        self.calls.append(url)
        return self.title, self.media_url


class SnowMDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise unittest.SkipTest("ffmpeg and ffprobe are required for SnowM fixtures")

        cls._fixture_tmp = tempfile.TemporaryDirectory(prefix="snowm-fixtures-")
        fixture_root = Path(cls._fixture_tmp.name)
        cls._source_mp4 = fixture_root / "source.mp4"
        cls._hls_dir = fixture_root / "hls"
        cls._hls_dir.mkdir()
        cls._playlist = cls._hls_dir / "playlist.m3u8"

        cls._run_ffmpeg(
            ffmpeg,
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=64x64:r=8:d=2",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "4",
                "-keyint_min",
                "4",
                "-sc_threshold",
                "0",
                "-movflags",
                "+faststart",
                str(cls._source_mp4),
            ],
        )
        cls._run_ffmpeg(
            ffmpeg,
            [
                "-i",
                str(cls._source_mp4),
                "-c",
                "copy",
                "-f",
                "hls",
                "-hls_time",
                "0.5",
                "-hls_list_size",
                "0",
                "-hls_segment_filename",
                str(cls._hls_dir / "segment_%02d.ts"),
                str(cls._playlist),
            ],
        )

        cls._server = _FixtureServer()
        source_bytes = cls._source_mp4.read_bytes()
        cls._server.start()
        cls._server.add_bytes(
            "/media/direct.mp4",
            source_bytes,
            content_type="video/mp4",
        )
        cls._server.add_bytes(
            "/media/query",
            source_bytes,
            content_type="video/mp4",
        )
        cls._server.add_bytes(
            "/media/extensionless",
            source_bytes,
            content_type="video/mp4",
        )
        cls._server.add_redirect(
            "/media/redirect",
            "/media/extensionless?token=redirected",
        )
        cls._server.add_bytes(
            "/web/browse/parser",
            (
                b'<html><body><h4>Parser demo</h4>'
                b'<a id="btn_play" href="/web/api/play">play</a>'
                b"</body></html>"
            ),
            content_type="text/html; charset=utf-8",
        )
        cls._server.add_bytes(
            "/web/api/play",
            b'{"playurl":"/media/extensionless?parser=1"}',
            content_type="application/json",
        )

        playlist_text = cls._playlist.read_text()
        segment_names = [
            line.strip()
            for line in playlist_text.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if not segment_names:
            cls._server.stop()
            cls._fixture_tmp.cleanup()
            raise unittest.SkipTest("ffmpeg did not produce HLS segments")
        cls._segment_names = segment_names
        cls._server.add_bytes(
            "/stream/playlist",
            playlist_text.encode(),
            content_type="text/plain",
        )
        cls._server.add_bytes(
            "/stream/missing-playlist",
            playlist_text.replace(segment_names[0], "missing-segment.ts", 1).encode(),
            content_type="text/plain",
        )
        for segment_name in segment_names:
            cls._server.add_bytes(
                f"/stream/{segment_name}",
                (cls._hls_dir / segment_name).read_bytes(),
                content_type="video/mp2t",
            )

    @staticmethod
    def _run_ffmpeg(ffmpeg: str, args: list[str]) -> None:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(f"ffmpeg fixture generation failed: {result.stderr}")

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "_server"):
            cls._server.stop()
        if hasattr(cls, "_fixture_tmp"):
            cls._fixture_tmp.cleanup()

    def _output_dir(self) -> Path:
        output_tmp = tempfile.TemporaryDirectory(prefix="snowm-output-")
        self.addCleanup(output_tmp.cleanup)
        return Path(output_tmp.name)

    def _new_downloader(
        self,
        output_dir: Path,
        title: str,
        media_url: str,
        *,
        verify_ssl: bool = True,
    ) -> tuple[Any, _StaticClient, Any]:
        video_module = importlib.import_module("toolbox.media.snowm.video")
        client = _StaticClient(title, media_url)
        with mock.patch.object(video_module, "SnowMClient", return_value=client):
            downloader = video_module.SnowMDownloader(
                str(output_dir),
                browser="chrome",
                verify_ssl=verify_ssl,
            )
        return downloader, client, video_module

    @contextlib.contextmanager
    def _quiet_ytdlp(self, video_module: Any, *, fragment_retries: int | None = None):
        real_youtube_dl = video_module.yt_dlp.YoutubeDL

        class QuietLogger:
            def debug(self, _message: str) -> None:
                return

            def info(self, _message: str) -> None:
                return

            def warning(self, _message: str) -> None:
                return

            def error(self, _message: str) -> None:
                return

        class QuietYoutubeDL:
            def __init__(self, params: dict[str, Any] | None = None, *args: Any, **kwargs: Any):
                adjusted = dict(params or {})
                adjusted.update(
                    {
                        "quiet": True,
                        "no_warnings": True,
                        "noprogress": True,
                        "logger": QuietLogger(),
                    }
                )
                if fragment_retries is not None:
                    adjusted["fragment_retries"] = fragment_retries
                    adjusted["retries"] = 0
                self._inner = real_youtube_dl(adjusted, *args, **kwargs)

            def __enter__(self) -> "QuietYoutubeDL":
                self._inner.__enter__()
                return self

            def __exit__(self, *args: Any) -> Any:
                return self._inner.__exit__(*args)

            def download(self, *args: Any, **kwargs: Any) -> Any:
                return self._inner.download(*args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        with mock.patch.object(video_module.yt_dlp, "YoutubeDL", QuietYoutubeDL):
            yield

    @staticmethod
    def _output_path(output_dir: Path, title: str) -> Path:
        filename = sanitize_filename(title).strip(" .") or "video"
        return output_dir / f"{filename}.mp4"

    def _assert_valid_mp4(self, path: Path) -> None:
        self.assertTrue(path.is_file(), f"expected completed output: {path}")
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=format_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("mp4", result.stdout)

    def test_downloads_direct_mp4_without_downloading_cookies(self) -> None:
        self._server.requests.clear()
        output_dir = self._output_dir()
        page_url = self._server.url("/web/browse/direct")
        downloader, client, video_module = self._new_downloader(
            output_dir,
            "Direct MP4",
            self._server.url("/media/direct.mp4"),
        )

        with self.assertLogs("toolbox.media.snowm.video", level="INFO") as logs:
            with contextlib.redirect_stdout(io.StringIO()) as terminal:
                downloader.download(page_url)

        messages = [record.getMessage() for record in logs.records]
        self.assertIn("\r[download]", terminal.getvalue())
        self.assertIn("100%", terminal.getvalue())
        self.assertEqual(messages[-1], f"视频 Direct MP4 处理完成 | URL: {page_url}")

        output_path = self._output_path(output_dir, "Direct MP4")
        self._assert_valid_mp4(output_path)
        self.assertIn(str(output_dir / ".tmp" / output_path.name), terminal.getvalue())
        self.assertEqual(list((output_dir / ".tmp").iterdir()), [])
        self.assertEqual(client.calls, [page_url])
        media_requests = [
            request
            for request in self._server.requests
            if request["path"].startswith("/media/")
        ]
        self.assertTrue(media_requests)
        for request in media_requests:
            self.assertFalse(
                any(key.lower() == "cookie" for key in request["headers"]),
                request,
            )
            self.assertEqual(request["headers"].get("Referer"), page_url)

    def test_parser_cookie_ends_before_yt_dlp_media_download(self) -> None:
        client_module = importlib.import_module("toolbox.media.snowm.client")
        cookie_jar = CookieJar()
        cookie_jar.set_cookie(
            create_cookie(
                name="parser_only",
                value="present",
                domain="127.0.0.1",
                path="/",
            )
        )
        self._server.requests.clear()
        page_url = self._server.url("/web/browse/parser")

        output_dir = self._output_dir()
        video_module = importlib.import_module("toolbox.media.snowm.video")
        with mock.patch.object(
            client_module,
            "extract_cookies_from_browser",
            return_value=cookie_jar,
        ) as cookie_loader:
            client = client_module.SnowMClient(
                browser="chrome",
                browser_profile="Profile 1",
            )
            with mock.patch.object(video_module, "SnowMClient") as client_factory:
                client_factory.return_value = client
                # Exercise the production wiring: the real parser instance is
                # the exact client held by SnowMDownloader during this download.
                downloader = video_module.SnowMDownloader(
                    str(output_dir),
                    browser="chrome",
                    browser_profile="Profile 1",
                )
            with self._quiet_ytdlp(video_module):
                downloader.download(page_url)

        cookie_loader.assert_called_once_with("chrome", profile="Profile 1")
        self._assert_valid_mp4(self._output_path(output_dir, "Parser demo"))
        parser_requests = [
            request
            for request in self._server.requests
            if request["path"].startswith("/web/")
        ]
        self.assertEqual(len(parser_requests), 2)
        for request in parser_requests:
            self.assertEqual(request["headers"].get("Cookie"), "parser_only=present")

        media_requests = [
            request
            for request in self._server.requests
            if request["path"].startswith("/media/")
        ]
        self.assertTrue(media_requests)
        for request in media_requests:
            self.assertFalse(
                any(key.lower() == "cookie" for key in request["headers"]),
                request,
            )

    def test_downloads_extensionless_hls_playlist(self) -> None:
        self._server.requests.clear()
        output_dir = self._output_dir()
        page_url = self._server.url("/web/browse/hls")
        downloader, _client, video_module = self._new_downloader(
            output_dir,
            "HLS clip",
            self._server.url("/stream/playlist"),
        )

        with contextlib.redirect_stdout(io.StringIO()) as terminal:
            downloader.download(page_url)

        self.assertIn("\r[download]", terminal.getvalue())
        self.assertIn("100%", terminal.getvalue())
        self.assertIn("(frag ", terminal.getvalue())
        output_path = self._output_path(output_dir, "HLS clip")
        self._assert_valid_mp4(output_path)
        self.assertIn(str(output_dir / ".tmp" / output_path.name), terminal.getvalue())
        self.assertEqual(list((output_dir / ".tmp").iterdir()), [])
        stream_requests = [
            request
            for request in self._server.requests
            if request["path"].startswith("/stream/")
        ]
        self.assertGreaterEqual(len(stream_requests), 2)
        for request in stream_requests:
            self.assertFalse(
                any(key.lower() == "cookie" for key in request["headers"]),
                request,
            )

    def test_media_urls_with_query_no_extension_and_redirect_are_supported(self) -> None:
        variants = (
            ("query", "/media/query?token=abc"),
            ("extensionless", "/media/extensionless"),
            ("redirect", "/media/redirect"),
        )
        for suffix, media_path in variants:
            with self.subTest(suffix=suffix):
                output_dir = self._output_dir()
                downloader, _client, video_module = self._new_downloader(
                    output_dir,
                    f"Media {suffix}",
                    self._server.url(media_path),
                )
                with self._quiet_ytdlp(video_module):
                    downloader.download(self._server.url(f"/web/browse/{suffix}"))
                self._assert_valid_mp4(
                    self._output_path(output_dir, f"Media {suffix}")
                )

    def test_title_percent_and_slash_remain_one_safe_filename(self) -> None:
        title = "100% ready / %(title)s"
        output_dir = self._output_dir()
        downloader, _client, video_module = self._new_downloader(
            output_dir,
            title,
            self._server.url("/media/direct.mp4"),
        )
        with self._quiet_ytdlp(video_module):
            downloader.download(self._server.url("/web/browse/safe-name"))

        output_path = self._output_path(output_dir, title)
        self._assert_valid_mp4(output_path)
        self.assertEqual(output_path.parent, output_dir)
        self.assertEqual(set(output_dir.iterdir()), {output_path, output_dir / ".tmp"})

    def test_completed_mp4_is_skipped_without_media_request(self) -> None:
        self._server.requests.clear()
        title = "Already downloaded"
        output_dir = self._output_dir()
        output_path = self._output_path(output_dir, title)
        output_path.write_bytes(b"existing completed file")
        page_url = self._server.url("/web/browse/already-downloaded")
        downloader, client, video_module = self._new_downloader(
            output_dir,
            title,
            self._server.url("/media/direct.mp4"),
        )

        with mock.patch.object(
            video_module.yt_dlp,
            "YoutubeDL",
            side_effect=AssertionError("yt-dlp should not run for completed output"),
        ):
            downloader.download(page_url)

        self.assertEqual(client.calls, [page_url])
        self.assertEqual(output_path.read_bytes(), b"existing completed file")
        self.assertFalse(
            any(request["path"].startswith("/media/") for request in self._server.requests)
        )

    def test_ssl_verification_can_be_disabled_for_media_download(self) -> None:
        openssl = shutil.which("openssl")
        if not openssl:
            self.skipTest("openssl is required for the self-signed HTTPS fixture")

        certificate_dir = self._output_dir()
        certificate = certificate_dir / "certificate.pem"
        key = certificate_dir / "key.pem"
        subprocess.run(
            [
                openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(certificate), "-days", "1",
                "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        server = _FixtureServer()
        server.add_bytes(
            "/media/video.mp4", self._source_mp4.read_bytes(), content_type="video/mp4",
        )
        server.start(context)
        self.addCleanup(server.stop)

        output_dir = self._output_dir()
        title = "Self-signed HTTPS"
        media_url = server.url("/media/video.mp4")
        page_url = server.url("/web/browse/tls")
        downloader, _client, video_module = self._new_downloader(output_dir, title, media_url)
        with self.assertLogs("toolbox.media.snowm.video", level="ERROR") as logs:
            with self.assertRaisesRegex(yt_dlp.utils.DownloadError, "CERTIFICATE_VERIFY_FAILED"):
                with self._quiet_ytdlp(video_module):
                    downloader.download(page_url)
        self.assertEqual(logs.records[-1].getMessage(), f"视频 {title} 下载失败 | URL: {page_url}")
        self.assertFalse(self._output_path(output_dir, title).exists())

        downloader, _client, video_module = self._new_downloader(
            output_dir, title, media_url, verify_ssl=False,
        )
        with self._quiet_ytdlp(video_module):
            downloader.download(page_url)
        self._assert_valid_mp4(self._output_path(output_dir, title))

    def test_missing_hls_fragment_aborts_without_completed_mp4(self) -> None:
        output_dir = self._output_dir()
        title = "Missing fragment"
        downloader, _client, video_module = self._new_downloader(
            output_dir,
            title,
            self._server.url("/stream/missing-playlist"),
        )

        page_url = self._server.url("/web/browse/missing")
        with self.assertLogs("toolbox.media.snowm.video", level="ERROR") as logs:
            with self.assertRaises((yt_dlp.utils.DownloadError, RuntimeError)):
                with self._quiet_ytdlp(video_module, fragment_retries=0):
                    downloader.download(page_url)
        self.assertEqual(logs.records[-1].getMessage(), f"视频 {title} 下载失败 | URL: {page_url}")

        self.assertFalse(self._output_path(output_dir, title).exists())
        self.assertEqual(list(output_dir.iterdir()), [output_dir / ".tmp"])
        self.assertTrue(list((output_dir / ".tmp").glob("*.part")))
        self.assertTrue(list((output_dir / ".tmp").glob("*.ytdl")))


if __name__ == "__main__":
    unittest.main()
