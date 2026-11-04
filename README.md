# workbench
个人自用工作台

## YouTube 视频和字幕

默认保存视频和独立的 VTT 字幕文件，例如 `视频标题.mp4` 和 `视频标题.zh-Hans.vtt`。字幕优先下载 VTT，保留原始字幕内容，不再转换为 SRT；默认不烧进画面或内嵌字幕轨道。

```python
from toolbox.media.youtube.video import YoutubeDownloader

downloader = YoutubeDownloader(output_dir="downloads")
downloader.download("https://www.youtube.com/watch?v=VIDEO_ID")
downloader.batch_download(
    ["https://www.youtube.com/watch?v=VIDEO_ID"],
    subtitle_languages=["zh-Hans", "en"],
)
```

默认匹配 `zh-Hans` / `zh-CN`，可通过 `subtitle_languages` 指定其他语言；每个匹配语言单独保存一份字幕。没有匹配字幕时，yt-dlp 会提示，不生成字幕文件。`download_subtitles=False` 可仅下载视频。

网站没有 VTT 时，yt-dlp 会提示并使用可用格式，保留其原格式。

通过 `hard_subtitles=True` 生成双语硬字幕：原文在上、中文在下，同组一起替换，并保留约 50% 不透明度的黑色背景。程序优先从 YouTube 原始字幕标记识别原文语言，也支持视频语言和唯一的上传者字幕语言；中文优先选择简体。两轨下载为 VTT，整理滚动重复并按时间配对后，生成 ASS 烧录，另保存 `视频标题.bilingual.ass`；独立 VTT 保持原样。

```python
downloader.download("https://www.youtube.com/watch?v=VIDEO_ID", hard_subtitles=True)

# 无法自动识别原文语言时，可明确指定，例如韩语原文与简体中文。
downloader.download(
    "https://www.youtube.com/watch?v=VIDEO_ID",
    subtitle_languages=["ko", "zh-Hans"],
    hard_subtitles=True,
)
```

硬字幕模式需要中文字幕提供 VTT；找不到或无法识别原文字幕时，仅烧录中文字幕。找到原文字幕时，原文也须提供 VTT，且两轨须能按时间配对，否则会报错。程序不自行翻译缺失的中文，也不从视频画面中提取已压制的原文。硬字幕模式下的多语言列表仅选择其中一组原文和中文。

配对使用两轨的时间交集；只有一轨的片段不显示，避免拼出无对应关系的双语。字幕内的多行会合并为一行，长行按宽度缩放。逐字字幕仍会随原时间更新文字，但不会将上一句滚到上方。

硬字幕输出 H.264/AAC MP4，需要重新编码，处理时间较长且无法关闭字幕；成功后替换下载的视频，失败时保留源文件。不能同时设置 `download_subtitles=False`。

对比测试请使用新的 `output_dir`，避免复用已烧过字幕的视频并再次叠加字幕。

只有硬字幕模式需要包含 libass `subtitles` 滤镜的 FFmpeg。macOS 可安装 `brew install ffmpeg-full`，并指定位置（Apple Silicon 示例）：

```python
downloader = YoutubeDownloader(
    output_dir="downloads",
    ffmpeg_location="/opt/homebrew/opt/ffmpeg-full/bin",
)
```

验证：`.venv/bin/python -m unittest tests.media.youtube.test_download -v`。硬字幕画面测试需要支持上述滤镜的 FFmpeg，可通过 `YOUTUBE_TEST_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` 指定。
