import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yt_dlp
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import DownloadError, ExtractorError, PostProcessingError

from toolbox.media.youtube.video import YoutubeDownloader, _BilingualSubtitlesPP, _FFmpegHardSubtitlesPP


class YoutubeDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_default_saves_separate_vtt(self):
        with mock.patch("toolbox.media.youtube.video.yt_dlp.YoutubeDL") as factory:
            YoutubeDownloader(str(self.root)).download("https://example.com/video")
        options = factory.call_args.args[0]
        self.assertNotIn("postprocessors", options)
        self.assertEqual(options["subtitlesformat"], "vtt")
        factory.return_value.__enter__.return_value.add_post_processor.assert_not_called()

    def test_subtitle_download_can_be_disabled(self):
        with mock.patch("toolbox.media.youtube.video.yt_dlp.YoutubeDL") as factory:
            YoutubeDownloader(str(self.root)).download("unused", download_subtitles=False)
        options = factory.call_args.args[0]
        self.assertNotIn("postprocessors", options)
        self.assertNotIn("writesubtitles", options)

    def test_hard_subtitles_registered_for_single_and_batch_downloads(self):
        for urls in (["first"], ["first", "second"]):
            with self.subTest(urls=urls), mock.patch(
                "toolbox.media.youtube.video.yt_dlp.YoutubeDL"
            ) as factory, mock.patch.object(_FFmpegHardSubtitlesPP, "check_available"):
                downloader = YoutubeDownloader(str(self.root), ffmpeg_location="/custom/ffmpeg")
                if len(urls) == 1:
                    downloader.download(urls[0], ["zh-Hans", "en"], hard_subtitles=True)
                else:
                    downloader.batch_download(urls, ["zh-Hans", "en"], hard_subtitles=True)
                options = factory.call_args.args[0]
                self.assertNotIn("postprocessors", options)
                self.assertEqual(options["subtitlesformat"], "vtt")
                self.assertEqual(options["subtitleslangs"], ["zh-Hans", "en"])
                self.assertEqual(options["ffmpeg_location"], "/custom/ffmpeg")
                ydl = factory.return_value.__enter__.return_value
                self.assertIsInstance(ydl.add_post_processor.call_args.args[0], _FFmpegHardSubtitlesPP)
                ydl.download.assert_called_once_with(urls)

    def test_disabled_subtitle_download_rejected_before_download(self):
        with mock.patch("toolbox.media.youtube.video.yt_dlp.YoutubeDL") as factory:
            with self.assertRaisesRegex(ValueError, "download_subtitles"):
                YoutubeDownloader(str(self.root)).download(
                    "unused", download_subtitles=False, hard_subtitles=True,
                )
            factory.assert_not_called()

    def test_missing_filter_rejected_before_download(self):
        result = subprocess.CompletedProcess([], 0, " .. scale V->V Scale video\n", "")
        with mock.patch("toolbox.media.youtube.video.yt_dlp.YoutubeDL") as factory, mock.patch.object(
            _FFmpegHardSubtitlesPP, "check_version"
        ), mock.patch.object(_FFmpegHardSubtitlesPP, "executable", "/ffmpeg"), mock.patch(
            "toolbox.media.youtube.video.subprocess.run", return_value=result,
        ):
            with self.assertRaisesRegex(PostProcessingError, "libass"):
                YoutubeDownloader(str(self.root)).download("unused", hard_subtitles=True)
            factory.return_value.__enter__.return_value.download.assert_not_called()

    def test_missing_subtitles_do_not_touch_source(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"original")
        processor = _FFmpegHardSubtitlesPP()
        for subtitles in ({}, {"zh-Hans": {"filepath": str(self.root / "missing.vtt")}}):
            with self.subTest(subtitles=subtitles), self.assertRaises(PostProcessingError):
                processor.run({"filepath": str(source), "requested_subtitles": subtitles})
            self.assertEqual(source.read_bytes(), b"original")

    def test_encoding_failure_preserves_source_and_subtitles(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"original")
        subtitle = self.root / "source.en.vtt"
        subtitle.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nOriginal\n", encoding="utf-8")
        chinese = self.root / "source.zh-Hans.vtt"
        chinese.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n中文\n", encoding="utf-8")
        processor = _FFmpegHardSubtitlesPP()

        def fail_encoding(source_path, output_path, options):
            Path(output_path).write_bytes(b"partial output")
            raise PostProcessingError("encoding failed")

        with mock.patch.object(processor, "run_ffmpeg", side_effect=fail_encoding):
            with self.assertRaisesRegex(PostProcessingError, "encoding failed"):
                processor.run({
                    "filepath": str(source),
                    "requested_subtitles": {
                        "en": {"filepath": str(subtitle)},
                        "zh-Hans": {"filepath": str(chinese)},
                    },
                })
        self.assertEqual(source.read_bytes(), b"original")
        self.assertTrue(subtitle.is_file())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [
            "source.en.vtt", "source.mp4", "source.zh-Hans.vtt",
        ])

    def test_video_replace_failure_restores_existing_ass(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"original")
        native = self.root / "source.en.vtt"
        chinese = self.root / "source.zh-Hans.vtt"
        for path, text in ((native, "Original"), (chinese, "中文")):
            path.write_text(f"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n{text}\n", encoding="utf-8")
        ass = source.with_suffix(".bilingual.ass")
        processor = _FFmpegHardSubtitlesPP()
        replace = Path.replace

        def fail_video_replace(path, target):
            if path.name == "encoded.mp4":
                raise OSError("cannot replace video")
            return replace(path, target)

        def encode(source_path, target, options):
            Path(target).write_bytes(b"encoded")

        for old_ass in (None, b"previous subtitles"):
            if old_ass is not None:
                ass.write_bytes(old_ass)
            with self.subTest(old_ass=old_ass), mock.patch.object(
                processor, "run_ffmpeg", side_effect=encode,
            ), mock.patch.object(Path, "replace", fail_video_replace):
                with self.assertRaisesRegex(PostProcessingError, "cannot replace video"):
                    processor.run({"filepath": str(source), "requested_subtitles": {
                        "en": {"filepath": str(native)}, "zh-Hans": {"filepath": str(chinese)},
                    }})
            self.assertEqual(source.read_bytes(), b"original")
            self.assertEqual(ass.read_bytes() if ass.exists() else None, old_ass)

    def test_unpairable_subtitles_preserve_source_without_encoding(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"original")
        native = self.root / "source.en.vtt"
        chinese = self.root / "source.zh-Hans.vtt"
        native.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nOriginal\n", encoding="utf-8")
        chinese.write_text("WEBVTT\n\n00:00:02.000 --> 00:00:03.000\n中文\n", encoding="utf-8")
        processor = _FFmpegHardSubtitlesPP()
        with mock.patch.object(processor, "run_ffmpeg") as encode:
            with self.assertRaisesRegex(PostProcessingError, "双语字幕生成失败"):
                processor.run({"filepath": str(source), "requested_subtitles": {
                    "en": {"filepath": str(native)}, "zh-Hans": {"filepath": str(chinese)},
                }})
            encode.assert_not_called()
        self.assertEqual(source.read_bytes(), b"original")
        self.assertFalse(source.with_suffix(".bilingual.ass").exists())

    def test_chinese_only_hard_subtitles(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"original")
        chinese = self.root / "source.zh-Hans.vtt"
        chinese.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n中文\n", encoding="utf-8")
        processor = _FFmpegHardSubtitlesPP()

        def encode(source_path, target, options):
            Path(target).write_bytes(b"encoded")

        with mock.patch.object(processor, "run_ffmpeg", side_effect=encode):
            processor.run({"filepath": str(source), "requested_subtitles": {
                "zh-Hans": {"filepath": str(chinese)},
            }})
        self.assertEqual(source.read_bytes(), b"encoded")
        ass = source.with_suffix(".bilingual.ass").read_text(encoding="utf-8")
        self.assertIn("中文", ass)
        self.assertNotIn(r"\N", ass)


class BilingualSelectionTests(unittest.TestCase):
    def track(self, marker):
        return [{"ext": "vtt", "url": f"https://example.com/{marker}.vtt"}]

    def test_automatic_original_language_instead_of_english_translation(self):
        info = {
            "automatic_captions": {
                "ko-orig": self.track("original"),
                "en": self.track("translated"),
                "zh-Hans": self.track("chinese"),
            },
        }
        _, result = _BilingualSubtitlesPP(None).run(info)
        self.assertEqual(list(result["requested_subtitles"]), ["ko-orig", "zh-Hans"])

    def test_manual_original_preferred_and_each_video_selects_its_own_language(self):
        processor = _BilingualSubtitlesPP(None)
        for language in ("en", "ko", "ja"):
            with self.subTest(language=language):
                manual = self.track("manual")
                info = {
                    "language": language,
                    "subtitles": {language: manual, "zh-Hans": self.track("chinese")},
                    "automatic_captions": {f"{language}-orig": self.track("auto")},
                }
                _, result = processor.run(info)
                self.assertEqual(list(result["requested_subtitles"]), [language, "zh-Hans"])
                self.assertEqual(result["requested_subtitles"][language]["url"], manual[0]["url"])

    def test_explicit_source_language_and_chinese_preference(self):
        info = {"subtitles": {key: self.track(key) for key in ("en", "ja", "zh-Hant", "zh-Hans")}}
        _, result = _BilingualSubtitlesPP(None, ["ja", "zh-Hant"]).run(info)
        self.assertEqual(list(result["requested_subtitles"]), ["ja", "zh-Hant"])

    def test_explicit_original_matches_orig_suffix_with_multiple_sources(self):
        info = {"automatic_captions": {key: self.track(key) for key in ("en-orig", "ja-orig", "zh-Hans")}}
        _, result = _BilingualSubtitlesPP(None, ["ja", "zh-Hans"]).run(info)
        self.assertEqual(list(result["requested_subtitles"]), ["ja-orig", "zh-Hans"])

    def test_known_video_language_does_not_fall_back_to_unrelated_manual_track(self):
        for language in ("ko", "zh"):
            info = {"language": language, "subtitles": {
                "en": self.track("english-translation"), "zh-Hans": self.track("chinese"),
            }}
            with self.subTest(language=language):
                _, result = _BilingualSubtitlesPP(None).run(info)
                self.assertEqual(list(result["requested_subtitles"]), ["zh-Hans"])

    def test_missing_chinese_and_missing_vtt_fail(self):
        cases = [
            {"subtitles": {"en": self.track("original")}},
            {"subtitles": {"zh-Hans": [{"ext": "srt", "url": "https://example.com/zh.srt"}]}},
            {"subtitles": {"en": [{"ext": "srt", "url": "https://example.com/en.srt"}],
                           "zh-Hans": self.track("chinese")}},
        ]
        for info in cases:
            with self.subTest(info=info), self.assertRaises(ExtractorError):
                _BilingualSubtitlesPP(None).run(info)

    def test_missing_or_ambiguous_original_uses_chinese_only(self):
        for subtitles in (
            {"zh-Hans": self.track("chinese")},
            {"en": self.track("en"), "ja": self.track("ja"), "zh-Hans": self.track("chinese")},
        ):
            with self.subTest(subtitles=subtitles):
                _, result = _BilingualSubtitlesPP(None).run({"subtitles": subtitles})
                self.assertEqual(list(result["requested_subtitles"]), ["zh-Hans"])

    def test_missing_track_stops_yt_dlp_before_video_download(self):
        class MissingChineseIE(InfoExtractor):
            _VALID_URL = r"missing:(?P<id>\w+)"

            def _real_extract(self, url):
                return {
                    "id": self._match_id(url), "title": "missing-chinese",
                    "url": "https://example.com/unused.mp4", "ext": "mp4",
                    "subtitles": {"en": [{"url": "https://example.com/en.vtt", "ext": "vtt"}]},
                }

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}, auto_init=False) as ydl:
            ydl.add_info_extractor(MissingChineseIE())
            ydl.add_post_processor(_BilingualSubtitlesPP(ydl), when="pre_process")
            with mock.patch.object(ydl, "process_info") as download:
                with self.assertRaises(DownloadError):
                    ydl.download(["missing:first"])
                download.assert_not_called()


class YoutubeVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = os.environ.get("YOUTUBE_TEST_FFMPEG") or shutil.which("ffmpeg")
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg is required for video integration tests")
    def require_hard_subtitles(self):
        with yt_dlp.YoutubeDL({"quiet": True, "ffmpeg_location": self.ffmpeg}) as ydl:
            try:
                _FFmpegHardSubtitlesPP(ydl).check_available()
            except PostProcessingError as error:
                raise unittest.SkipTest(str(error)) from error

    def ffmpeg_run(self, *args):
        return subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args],
            check=True, capture_output=True,
        ).stdout

    def test_default_batch_download_saves_video_and_vtt(self):
        self.assert_batch_download(hard_subtitles=False)

    def test_batch_download_burns_subtitles_and_keeps_vtt(self):
        self.require_hard_subtitles()
        self.assert_batch_download(hard_subtitles=True)

    def assert_batch_download(self, *, hard_subtitles):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "fixture.mp4"
            audio = root / "fixture.m4a"
            subtitle = root / "fixture.vtt"
            self.ffmpeg_run(
                "-f", "lavfi", "-i", "color=c=black:s=640x360:r=10:d=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
            )
            self.ffmpeg_run(
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", str(audio),
            )
            subtitle.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000 align:start position:10%\n"
                "Hard <00:00:00.200>subtitle\n", encoding="utf-8",
            )
            srt_subtitle = root / "fixture.srt"
            srt_subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nUnused SRT\n", encoding="utf-8",
            )
            chinese = root / "fixture.zh.vtt"
            chinese.write_text(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n中文字幕测试\n", encoding="utf-8",
            )

            class FixtureIE(InfoExtractor):
                _VALID_URL = r"fixture:(?P<id>\w+)"

                def _real_extract(self, url):
                    identifier = self._match_id(url)
                    language = "ko" if hard_subtitles and identifier == "second" else "en"
                    return {
                        "id": identifier, "title": identifier,
                        "language": language,
                        "formats": [
                            {"format_id": "audio", "url": audio.as_uri(), "ext": "m4a",
                             "vcodec": "none", "acodec": "aac"},
                            {"format_id": "video", "url": video.as_uri(), "ext": "mp4",
                             "vcodec": "h264", "acodec": "none"},
                        ],
                        "subtitles": {language: [
                            {"url": subtitle.as_uri(), "ext": "vtt"},
                            {"url": srt_subtitle.as_uri(), "ext": "srt"},
                        ], "zh-Hans": [{"url": chinese.as_uri(), "ext": "vtt"}]},
                    }

            real_ydl = yt_dlp.YoutubeDL

            def fixture_downloader(options):
                ydl = real_ydl(
                    {**options, "enable_file_urls": True, "quiet": True, "noprogress": True},
                    auto_init=False,
                )
                ydl.add_info_extractor(FixtureIE())
                return ydl

            output = root / "downloads"
            with mock.patch("toolbox.media.youtube.video.yt_dlp.YoutubeDL", side_effect=fixture_downloader):
                YoutubeDownloader(str(output), ffmpeg_location=self.ffmpeg).batch_download(
                    ["fixture:first", "fixture:second"],
                    None if hard_subtitles else ["en"], hard_subtitles=hard_subtitles,
                )
            second_language = "ko" if hard_subtitles else "en"
            expected = ["first.en.vtt", "first.mp4", f"second.{second_language}.vtt", "second.mp4"]
            if hard_subtitles:
                expected += ["first.zh-Hans.vtt", "second.zh-Hans.vtt",
                             "first.bilingual.ass", "second.bilingual.ass"]
            self.assertEqual(sorted(path.name for path in output.iterdir()), sorted(expected))
            if hard_subtitles:
                for path in output.glob("*.ass"):
                    content = path.read_text(encoding="utf-8")
                    self.assertIn("Hard subtitle\\N中文字幕测试", content)
                    self.assertIn("WrapStyle: 2", content)
            for path in output.glob("*.vtt"):
                original = chinese if path.name.endswith(".zh-Hans.vtt") else subtitle
                self.assertEqual(path.read_bytes(), original.read_bytes())
            for path in output.glob("*.mp4"):
                pixels = self.ffmpeg_run(
                    "-ss", "0.5", "-i", str(path), "-frames:v", "1",
                    "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
                )
                if hard_subtitles:
                    self.assertGreater(sum(pixel > 200 for pixel in pixels), 50)
                else:
                    self.assertLessEqual(max(pixels), 5)
                probe = str(Path(self.ffmpeg).with_name("ffprobe"))
                metadata = json.loads(subprocess.run(
                    [probe, "-v", "error", "-show_streams", "-of", "json", str(path)],
                    capture_output=True, text=True, check=True,
                ).stdout)
                self.assertEqual([stream["codec_type"] for stream in metadata["streams"]], ["video", "audio"])

    def test_subtitles_are_pixels_in_mp4_with_and_without_audio(self):
        self.require_hard_subtitles()
        for extension, with_audio in (("mp4", True), ("webm", False)):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temp_dir:
                # Exercise Chinese names and both layers of FFmpeg filter escaping.
                root = Path(temp_dir) / "中文 'quoted' [test],semi;colon:back\\slash"
                root.mkdir()
                source = root / f"video.{extension}"
                inputs = ["-f", "lavfi", "-i", "color=c=black:s=640x360:r=10:d=1"]
                if with_audio:
                    inputs += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
                self.ffmpeg_run(*inputs, "-c:v", "libx264" if extension == "mp4" else "libvpx-vp9",
                                "-pix_fmt", "yuv420p", str(source))
                subtitle = root / "video.en.vtt"
                subtitle.write_text(
                    "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHard subtitle 测试\n", encoding="utf-8",
                )
                other_subtitle = root / "video.zh-Hans.vtt"
                other_subtitle.write_text(
                    "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n中文字幕测试\n", encoding="utf-8",
                )
                with yt_dlp.YoutubeDL({"quiet": True, "ffmpeg_location": self.ffmpeg}) as ydl:
                    info = ydl.run_pp(_FFmpegHardSubtitlesPP(ydl), {
                        "filepath": str(source), "ext": extension,
                        "requested_subtitles": {
                            "en": {"filepath": str(subtitle)},
                            "zh-Hans": {"filepath": str(other_subtitle)},
                        },
                    })
                output = Path(info["filepath"])
                self.assertEqual(output.suffix, ".mp4")
                self.assertEqual(info["ext"], "mp4")
                self.assertTrue(subtitle.is_file())
                self.assertTrue(other_subtitle.is_file())
                self.assertEqual(set(root.iterdir()), {
                    output, subtitle, other_subtitle, output.with_suffix(".bilingual.ass"),
                })
                probe = str(Path(self.ffmpeg).with_name("ffprobe"))
                metadata = json.loads(subprocess.run(
                    [probe, "-v", "error", "-show_streams", "-of", "json", str(output)],
                    capture_output=True, text=True, check=True,
                ).stdout)
                stream_types = [stream["codec_type"] for stream in metadata["streams"]]
                self.assertEqual(stream_types, ["video", "audio"] if with_audio else ["video"])
                pixels = self.ffmpeg_run(
                    "-ss", "0.5", "-i", str(output), "-frames:v", "1",
                    "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
                )
                # The input is black; bright pixels must come from rendered text.
                self.assertGreater(sum(pixel > 200 for pixel in pixels), 50)
