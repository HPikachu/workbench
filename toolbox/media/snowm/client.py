"""Client for resolving SnowM video pages to media URLs.

The browser cookie jar is used only for the SnowM page and playback API
requests.  The returned URL is deliberately format-neutral: it may point to
an HLS playlist or to a directly downloadable media file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from yt_dlp.cookies import extract_cookies_from_browser

DEFAULT_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "priority": "u=0, i",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
}

DEFAULT_TIMEOUT = 30


class SnowMClientError(RuntimeError):
    """Raised when a SnowM page or playback response cannot be used."""


class SnowMClient:
    """Resolve SnowM detail pages to an HLS or direct media URL.

    Cookies are loaded lazily from the selected browser profile and kept in a
    private :class:`requests.Session`.  They are therefore used for parser
    requests only; callers can pass the returned URL to a separate downloader
    without sharing the browser credentials.
    """

    def __init__(
            self,
            browser: Optional[str] = "chrome",
            browser_profile: Optional[str] = None,
            *,
            timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.browser = browser
        self.browser_profile = browser_profile
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(DEFAULT_HEADERS)
        self._cookies_loaded = False

    def _load_cookies(self) -> None:
        if self._cookies_loaded:
            return

        if self.browser is None:
            self._cookies_loaded = True
            return

        try:
            cookie_jar = extract_cookies_from_browser(
                self.browser,
                profile=self.browser_profile,
            )
            if cookie_jar is None:
                raise ValueError("浏览器没有返回 cookie jar")
            # RequestsCookieJar.update(CookieJar) retains cookie domain, path,
            # and expiry, which is required when the same name exists on more
            # than one SnowM path.
            self._session.cookies.update(cookie_jar)
        except Exception as exc:
            raise SnowMClientError(
                f"无法从浏览器 {self.browser!r} 读取 SnowM cookies；"
                "请确认浏览器配置文件正确且已完成登录，重新读取后再试。"
            ) from exc

        self._cookies_loaded = True

    def refresh_cookies(self) -> None:
        """Reload cookies after the user has logged in again in the browser."""

        self._session.cookies.clear()
        self._cookies_loaded = False
        self._load_cookies()

    def _request(self, url: str, description: str) -> requests.Response:
        self._load_cookies()
        response = None

        try:
            response = self._session.get(url, timeout=self.timeout)
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is None:
                status = getattr(response, "status_code", "unknown")
            raise SnowMClientError(
                f"SnowM {description}请求返回 HTTP {status}：{url}；"
                "请检查 URL、页面访问权限和浏览器登录状态。"
            ) from exc
        except requests.RequestException as exc:
            raise SnowMClientError(
                f"SnowM {description}请求失败：{url}；"
                "请检查网络连接和 URL。"
            ) from exc

        return response

    @staticmethod
    def _resolve_url(value: str, base_url: Optional[str], description: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise SnowMClientError(f"SnowM 响应缺少有效的{description}。")

        resolved = urljoin(base_url or "", value.strip())
        parsed = urlparse(resolved)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SnowMClientError(
                f"SnowM 返回的{description}无效：{value!r}；"
                "请检查页面或播放接口返回的数据。"
            )
        return resolved

    @staticmethod
    def _response_url(response: requests.Response, fallback: str) -> str:
        response_url = getattr(response, "url", None)
        if not isinstance(response_url, str) or not response_url.strip():
            response_url = fallback
        return SnowMClient._resolve_url(response_url, fallback, "响应 URL")

    @staticmethod
    def _response_json(response: requests.Response) -> object:
        try:
            return response.json()
        except (AttributeError, TypeError, ValueError):
            try:
                return json.loads(response.text)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SnowMClientError(
                    "SnowM 播放接口返回的内容不是有效 JSON；"
                    "请检查播放接口地址和当前登录状态。"
                ) from exc

    def get_video_info(self, web_url: str) -> tuple[str, str]:
        """Return the page title and its HLS or direct media URL."""

        page_url = self._resolve_url(web_url, None, "视频详情页 URL")
        response = self._request(page_url, "视频详情页")
        response_url = self._response_url(response, page_url)

        try:
            soup = BeautifulSoup(response.text, "lxml")
        except Exception as exc:
            raise SnowMClientError(
                "无法解析 SnowM 视频详情页；请检查页面内容和 HTML 解析依赖。"
            ) from exc

        title_node = soup.find("h4")
        title = title_node.get_text().strip() if title_node else ""
        if not title:
            raise SnowMClientError(
                "SnowM 视频详情页缺少有效标题（h4）；"
                "请确认 URL 指向视频详情页及浏览器登录状态，并检查页面是否被重定向。"
            )

        play_node = soup.find(id="btn_play")
        endpoint = play_node.get("href") if play_node else None
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise SnowMClientError(
                "SnowM 视频详情页缺少播放入口（#btn_play[href]）；"
                "请确认页面结构、浏览器登录状态和当前账号的访问权限。"
            )

        endpoint_url = self._resolve_url(endpoint, response_url, "播放接口 URL")
        return title, self.get_media_url(endpoint_url)

    def get_media_url(self, endpoint_url: str) -> str:
        """Return the media URL from a SnowM playback API response."""

        endpoint = self._resolve_url(endpoint_url, None, "播放接口 URL")
        response = self._request(endpoint, "播放接口")
        response_url = self._response_url(response, endpoint)
        payload = self._response_json(response)

        if not isinstance(payload, Mapping):
            raise SnowMClientError(
                "SnowM 播放接口返回的数据结构无效，缺少 playurl 字段；"
                "请检查接口响应。"
            )

        playurl = payload.get("playurl")
        if not isinstance(playurl, str) or not playurl.strip():
            raise SnowMClientError(
                "SnowM 播放接口缺少有效 playurl；"
                "请确认视频已解锁并检查接口响应。"
            )

        return self._resolve_url(playurl, response_url, "媒体 URL")


__all__ = ["SnowMClient", "SnowMClientError"]
