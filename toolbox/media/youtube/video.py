import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Callable, List, Any

import yt_dlp
from yt_dlp.postprocessor import PostProcessor
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor
from yt_dlp.utils import ExtractorError, PostProcessingError

from .subtitles import build_bilingual_ass


def _is_chinese(language):
    return language.lower().split("-")[0] == "zh"


class _BilingualSubtitlesPP(PostProcessor):
    """Select two VTT tracks before yt-dlp writes subtitles or downloads video."""

    def __init__(self, downloader, languages=None):
        super().__init__(downloader)
        self.languages = languages or []

    def run(self, info):
        manual = info.get("subtitles") or {}
        available = {**(info.get("automatic_captions") or {}), **manual}
        available = {key: formats for key, formats in available.items() if formats and key != "live_chat"}

        original = None
        for pattern in self.languages:
            original = next((key for key in available
                             if not _is_chinese(key) and (
                                 re.fullmatch(pattern, key) or re.fullmatch(pattern, key.removesuffix("-orig"))
                             )), None)
            if original:
                break
        if not original:
            originals = [key for key in available if key.endswith("-orig") and not _is_chinese(key)]
            language = (info.get("language") or "").split("-")[0]
            if len(originals) == 1:
                original = originals[0]
            elif language and not _is_chinese(language):
                original = next((key for key in originals if key.split("-")[0] == language), None)
                original = original or next((key for key in available
                                             if key == language or key == info.get("language")), None)
            if not original and not originals and not language:
                candidates = [key for key in manual if key in available and not _is_chinese(key)]
                if len({key.split("-")[0] for key in candidates}) == 1:
                    original = candidates[0]
            if original and original.endswith("-orig") and original[:-5] in manual:
                original = original[:-5]
        chinese = None
        for pattern in self.languages:
            chinese = next((key for key in available
                            if _is_chinese(key) and re.fullmatch(pattern, key)), None)
            if chinese:
                break
        chinese = chinese or next((key for key in ("zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW")
                                    if key in available), None)
        chinese = chinese or next((key for key in available if _is_chinese(key)), None)
        if not chinese:
            raise ExtractorError("缺少中文字幕，无法生成原文和中文双语硬字幕。", expected=True)

        selected = {}
        for key in (key for key in (original, chinese) if key):
            track = next((item for item in reversed(available[key]) if item.get("ext") == "vtt"), None)
            if not track:
                raise ExtractorError(f"{key} 没有 VTT 字幕，无法生成双语硬字幕。", expected=True)
            selected[key] = dict(track)
        info["requested_subtitles"] = selected
        self.to_screen(f"Selected subtitles: {' + '.join(selected)}")
        return [], info


def _subtitle_filter_path(path):
    # Escape both the option value and the surrounding FFmpeg filtergraph.
    value = re.sub(r"([\\':])", r"\\\1", str(path.resolve()))
    return re.sub(r"([\\'\[\],;])", r"\\\1", value)


class _FFmpegHardSubtitlesPP(FFmpegPostProcessor):
    def check_available(self) -> None:
        self.check_version()
        result = subprocess.run(
            [self.executable, "-hide_banner", "-filters"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode or not re.search(r"^\s*\S+\s+subtitles\s", result.stdout, re.MULTILINE):
            raise PostProcessingError(
                "硬字幕模式需要支持 libass subtitles 滤镜的 FFmpeg。"
                "macOS 可安装 ffmpeg-full，并通过 ffmpeg_location 指定其路径。"
            )

    def run(self, info):
        subtitles = info.get("requested_subtitles") or {}
        if not subtitles:
            raise PostProcessingError("没有匹配的字幕，无法生成硬字幕视频；请检查 subtitle_languages。")

        original = next((key for key in subtitles if not _is_chinese(key)), None)
        chinese = next((key for key in subtitles if _is_chinese(key)), None)
        if not chinese:
            raise PostProcessingError("硬字幕需要中文字幕。")
        paths = [Path(subtitles[key].get("filepath") or "") for key in (original, chinese) if key]
        if any(not path.is_file() for path in paths):
            raise PostProcessingError("字幕文件不存在，无法生成双语硬字幕视频。")

        source = Path(info["filepath"])
        destination = source.with_suffix(".mp4")
        self.to_screen(f'Burning {" + ".join(key for key in (original, chinese) if key)} subtitles into "{destination}"')
        try:
            # Encode beside the source so replacement is atomic; retain it on failure.
            with tempfile.TemporaryDirectory(prefix=".youtube-hard-", dir=source.parent) as temp_dir:
                temp_dir = Path(temp_dir)
                temp_subtitle = temp_dir / "bilingual.ass"
                try:
                    texts = [path.read_text(encoding="utf-8-sig") for path in paths]
                    content = build_bilingual_ass(texts[0] if original else None, texts[-1])
                except (ValueError, UnicodeError) as error:
                    raise PostProcessingError(f"双语字幕生成失败：{error}") from error
                temp_subtitle.write_text(content, encoding="utf-8")
                subtitle_filter = f"subtitles=filename={_subtitle_filter_path(temp_subtitle)}"
                # macOS FFmpeg may not discover system CJK fonts through fontconfig.
                cjk_font = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
                if cjk_font.is_file():
                    fonts_dir = temp_dir / "fonts"
                    fonts_dir.mkdir()
                    shutil.copyfile(cjk_font, fonts_dir / "ArialUnicode.ttf")
                    subtitle_filter += f":fontsdir={_subtitle_filter_path(fonts_dir)}"
                encoded = temp_dir / "encoded.mp4"
                self.run_ffmpeg(str(source), str(encoded), [
                    "-map", "0:v:0", "-map", "0:a?", "-sn",
                    "-vf", subtitle_filter,
                    "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                ])
                ass_destination = destination.with_suffix(".bilingual.ass")
                ass_backup = temp_dir / "previous.ass"
                if ass_destination.is_file():
                    shutil.copyfile(ass_destination, ass_backup)
                temp_subtitle.replace(ass_destination)
                try:
                    encoded.replace(destination)
                except OSError:
                    if ass_backup.is_file():
                        ass_backup.replace(ass_destination)
                    else:
                        ass_destination.unlink()
                    raise
        except OSError as error:
            raise PostProcessingError(f"硬字幕视频处理失败：{error}") from error

        info["filepath"] = str(destination)
        info["ext"] = "mp4"
        info.setdefault("__files_to_move", {})[str(ass_destination)] = None
        files_to_delete = [str(source)] if source != destination else []
        return files_to_delete, info


class YoutubeDownloader(object):
    def __init__(
            self,
            output_dir: str,
            browser: Optional[str] = None,
            progress_callback: Optional[Callable[[dict], None]] = None,
            *,
            ffmpeg_location: Optional[str] = None,
    ):
        self.output_dir = Path(output_dir)
        self.browser = browser
        self.progress_callback = progress_callback
        self.ffmpeg_location = ffmpeg_location

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

        if self.ffmpeg_location:
            options["ffmpeg_location"] = self.ffmpeg_location

        return options

    def download(
            self,
            url: str,
            subtitle_languages: Optional[List[str]] = None,
            download_subtitles: bool = True,
            verbose: bool = False,
            *,
            hard_subtitles: bool = False,
    ) -> None:
        """Download VTT files, optionally burning original + Chinese subtitles into MP4."""
        self.batch_download(
            [url],
            subtitle_languages,
            download_subtitles,
            verbose,
            hard_subtitles=hard_subtitles,
        )

    def batch_download(
            self,
            urls: List[str],
            subtitle_languages: Optional[List[str]] = None,
            download_subtitles: bool = True,
            verbose: bool = False,
            *,
            hard_subtitles: bool = False,
    ):
        """Download videos and VTT files; bilingual hard subtitles require libass."""
        if hard_subtitles and not download_subtitles:
            raise ValueError("hard_subtitles=True 时必须启用 download_subtitles。")

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
                "subtitlesformat": "vtt",
            })

        with yt_dlp.YoutubeDL(options) as ydl:
            if hard_subtitles:
                processor = _FFmpegHardSubtitlesPP(ydl)
                processor.check_available()
                ydl.add_post_processor(_BilingualSubtitlesPP(ydl, subtitle_languages), when="pre_process")
                ydl.add_post_processor(processor)
            ydl.download(urls)
