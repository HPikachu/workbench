import logging
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import sanitize_filename

from .client import SnowMClient

logger = logging.getLogger(__name__)


class SnowMDownloader:
    def __init__(
            self,
            output_dir: str,
            browser: str = "chrome",
            browser_profile: str | None = None,
            *,
            verify_ssl: bool = True,
    ):
        self.output_dir = Path(output_dir).expanduser()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._verify_ssl = verify_ssl
        self._client = SnowMClient(browser=browser, browser_profile=browser_profile)

    def download(self, url: str) -> None:
        try:
            title, media_url = self._client.get_video_info(url)
        except Exception:
            logger.exception("SnowM 下载失败 | URL: %s", url)
            raise
        filename = sanitize_filename(title).strip(" .") or "video"
        mp4_path = self.output_dir / f"{filename}.mp4"
        if mp4_path.is_file():
            logger.info("视频 %s 已经下载完成 | URL: %s", title, url)
            return

        options: dict[str, Any] = {
            "paths": {"home": str(self.output_dir), "temp": ".tmp"},
            # A SnowM title may contain percent signs or yt-dlp template syntax.
            "outtmpl": filename.replace("%", "%%") + ".%(ext)s",
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "postprocessors": [{
                "key": "FFmpegVideoRemuxer",
                "preferedformat": "mp4",
            }],
            "concurrent_fragment_downloads": 30,
            "fragment_retries": 25,
            "skip_unavailable_fragments": False,
            "noplaylist": True,
            # A custom logger would turn native terminal progress into log lines.
            "noprogress": False,
            "progress_with_newline": False,
            "progress_delta": 1,
            "nocheckcertificate": not self._verify_ssl,
            "http_headers": {
                "Referer": url,
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
            },
        }
        # Browser/login cookies are owned by the parser, never by this downloader.
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                if downloader.download([media_url]):
                    raise RuntimeError(f"视频 {title} 下载失败")
        except Exception:
            logger.exception("视频 %s 下载失败 | URL: %s", title, url)
            raise

        logger.info("视频 %s 处理完成 | URL: %s", title, url)
