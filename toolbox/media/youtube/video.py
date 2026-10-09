from pathlib import Path
from typing import Optional, Callable, List, Any

import yt_dlp


class YoutubeDownloader(object):
    def __init__(
            self,
            output_dir: str,
            browser: Optional[str] = None,
            progress_callback: Optional[Callable[[dict], None]] = None
    ):
        self.output_dir = Path(output_dir)
        self.browser = browser
        self.progress_callback = progress_callback

        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _build_base_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "outtmpl": str(self.output_dir / "%(title)s.%(ext)s"),
            "extractor_args": {
                "youtube": {
                    "player_client": [
                        "default",
                        "web_embedded",
                    ]
                }
            }
        }

        if self.browser:
            options["cookiesfrombrowser"] = (self.browser,)

        if self.progress_callback:
            options["progress_hooks"] = [self.progress_callback]

        return options

    def download(
            self,
            url: str,
            subtitle_languages: Optional[List[str]] = None,
            download_subtitles: bool = True,
            verbose: bool = False,
    ) -> None:
        self.batch_download(
            [url],
            subtitle_languages,
            download_subtitles,
            verbose
        )

    def batch_download(
            self,
            urls: List[str],
            subtitle_languages: Optional[List[str]] = None,
            download_subtitles: bool = True,
            verbose: bool = False,
    ):

        options = self._build_base_options()

        options["verbose"] = verbose

        options.update({
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
        })

        if download_subtitles:
            options.update({
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": subtitle_languages or [r"zh-(?:Hans|CN)"],
                "postprocessors": [{
                    "key": "FFmpegEmbedSubtitle",
                    "already_have_subtitle": False,
                }],
            })

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download(urls)
