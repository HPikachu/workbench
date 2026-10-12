import unittest
from http.cookiejar import CookieJar
from unittest.mock import Mock, call, patch

import requests
from requests.cookies import create_cookie

from toolbox.media.snowm.client import (
    DEFAULT_HEADERS,
    SnowMClient,
    SnowMClientError,
)


def make_response(*, url, text="", payload=None, payload_is_set=False):
    response = Mock()
    response.url = url
    response.text = text
    response.raise_for_status.return_value = None
    if payload_is_set:
        response.json.return_value = payload
    return response


class SnowMClientTests(unittest.TestCase):
    def test_browser_cookies_are_loaded_once_with_profile_and_keep_scope(self):
        cookie_jar = CookieJar()
        cookie_jar.set_cookie(
            create_cookie(
                name="session",
                value="abc",
                domain="snowm88.com",
                path="/web",
                expires=1_900_000_000,
            )
        )

        client = SnowMClient(browser="chrome", browser_profile="Profile 2")
        with patch(
            "toolbox.media.snowm.client.extract_cookies_from_browser",
            return_value=cookie_jar,
        ) as extract:
            client._load_cookies()
            client._load_cookies()

        extract.assert_called_once_with("chrome", profile="Profile 2")
        cookies = list(client._session.cookies)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0].name, "session")
        self.assertEqual(cookies[0].domain, "snowm88.com")
        self.assertEqual(cookies[0].path, "/web")
        self.assertEqual(cookies[0].expires, 1_900_000_000)
        self.assertEqual(client._session.headers["user-agent"], DEFAULT_HEADERS["user-agent"])

    def test_refresh_cookies_reads_browser_again(self):
        first = CookieJar()
        first.set_cookie(create_cookie("session", "first", domain="snowm88.com"))
        second = CookieJar()
        second.set_cookie(create_cookie("session", "second", domain="snowm88.com"))

        client = SnowMClient(browser="chrome")
        with patch(
            "toolbox.media.snowm.client.extract_cookies_from_browser",
            side_effect=[first, second],
        ) as extract:
            client._load_cookies()
            client.refresh_cookies()

        self.assertEqual(list(client._session.cookies)[0].value, "second")
        self.assertEqual(extract.call_count, 2)

    def test_get_video_info_resolves_relative_endpoint_and_hls_media_url(self):
        page = make_response(
            url="https://www.snowm88.com/web/video/42",
            text='<html><h4> 示例 <span>视频</span> </h4><a id="btn_play" href="/api/play.php?id=42">播放</a></html>',
        )
        playback = make_response(
            url="https://api.snowm88.com/play.php?id=42",
            payload={"playurl": "//cdn.snowm88.com/media/video.m3u8"},
            payload_is_set=True,
        )
        client = SnowMClient(browser=None)
        client._session.get = Mock(side_effect=[page, playback])

        title, media_url = client.get_video_info("https://www.snowm88.com/web/video/42")

        self.assertEqual(title, "示例 视频")
        self.assertEqual(media_url, "https://cdn.snowm88.com/media/video.m3u8")
        self.assertEqual(
            client._session.get.call_args_list,
            [
                call("https://www.snowm88.com/web/video/42", timeout=30),
                call("https://www.snowm88.com/api/play.php?id=42", timeout=30),
            ],
        )

    def test_get_video_info_accepts_direct_mp4_media_url(self):
        page = make_response(
            url="https://www.snowm88.com/web/video/42",
            text='<h4>MP4 视频</h4><a id="btn_play" href="/api/play.php?id=42">播放</a>',
        )
        playback = make_response(
            url="https://www.snowm88.com/api/play.php?id=42",
            payload={"playurl": "media/video.mp4?token=abc"},
            payload_is_set=True,
        )
        client = SnowMClient(browser=None)
        client._session.get = Mock(side_effect=[page, playback])

        title, media_url = client.get_video_info("https://www.snowm88.com/web/video/42")

        self.assertEqual(title, "MP4 视频")
        self.assertEqual(
            media_url,
            "https://www.snowm88.com/api/media/video.mp4?token=abc",
        )

    def test_get_media_url_uses_json_text_fallback(self):
        response = make_response(
            url="https://www.snowm88.com/api/play.php",
            text='{"playurl": "https://cdn.snowm88.com/video.mp4"}',
        )
        response.json.side_effect = ValueError("invalid json in mock")
        client = SnowMClient(browser=None)
        client._session.get = Mock(return_value=response)

        self.assertEqual(
            client.get_media_url("https://www.snowm88.com/api/play.php"),
            "https://cdn.snowm88.com/video.mp4",
        )

    def test_missing_page_fields_raise_actionable_errors(self):
        missing_title = make_response(
            url="https://www.snowm88.com/web/video/42",
            text='<a id="btn_play" href="/api/play.php">播放</a>',
        )
        client = SnowMClient(browser=None)
        client._session.get = Mock(return_value=missing_title)
        with self.assertRaisesRegex(SnowMClientError, "缺少有效标题"):
            client.get_video_info("https://www.snowm88.com/web/video/42")

        missing_play = make_response(
            url="https://www.snowm88.com/web/video/42",
            text="<h4>示例视频</h4>",
        )
        client = SnowMClient(browser=None)
        client._session.get = Mock(return_value=missing_play)
        with self.assertRaisesRegex(SnowMClientError, "缺少播放入口"):
            client.get_video_info("https://www.snowm88.com/web/video/42")

    def test_invalid_playurl_is_rejected(self):
        response = make_response(
            url="https://www.snowm88.com/api/play.php",
            payload={"playurl": "javascript:alert(1)"},
            payload_is_set=True,
        )
        client = SnowMClient(browser=None)
        client._session.get = Mock(return_value=response)

        with self.assertRaisesRegex(SnowMClientError, "媒体 URL.*无效"):
            client.get_media_url("https://www.snowm88.com/api/play.php")

    def test_http_errors_are_wrapped_with_request_context(self):
        response = make_response(
            url="https://www.snowm88.com/web/video/42",
            text="forbidden",
        )
        response.status_code = 403
        response.raise_for_status.side_effect = requests.HTTPError(
            "403 Client Error", response=response
        )
        client = SnowMClient(browser=None)
        client._session.get = Mock(return_value=response)

        with self.assertRaisesRegex(SnowMClientError, "视频详情页请求返回 HTTP 403"):
            client.get_video_info("https://www.snowm88.com/web/video/42")

    def test_invalid_input_url_is_rejected_before_request(self):
        client = SnowMClient(browser=None)
        client._session.get = Mock()

        with self.assertRaises(SnowMClientError):
            client.get_video_info("/web/video/42")

        client._session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
