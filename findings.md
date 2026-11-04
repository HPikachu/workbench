# Findings

- 当前分支 feat/youtube-hard-subtitles，原有 README、video.py、tests/media/youtube 未提交。
- 本会话没有 codebase-memory MCP 工具，无法确认图谱项目/代次或覆盖；已直接读取相关源文件。
- yt-dlp 在 pre_process 前计算 requested_subtitles；pre_process 可改选字幕，发生在写字幕和视频下载之前。
- 现有硬字幕仅烧录首个匹配字幕，VTT 无处理直接交给 FFmpeg。
- 用户授权实现固定双语：原文在上、中文在下，保留半透明黑底。
- 当前输出目录没有可用 VTT；需要用覆盖滚动、时间偏移和不同语种的离线样例验证。
- 已新增 pre_process 选择器：按显式语言、原始 -orig 标记、视频语言或唯一上传者原文选轨，仅下载两轨VTT，缺轨提前报错。
- macOS FFmpeg默认字体发现不完整；烧录时将系统Arial Unicode.ttf加载到临时字体目录，确保中文和其他语种可见。
- 已委派独立subtitles.py模块和单元测试；父负责yt-dlp边界、集成及文档。
- 离线实际渲染：韩文原文与中文两行正常显示，无缺字；输入A->A/B滚动转换为两组ASS事件0-2/2-4，整体替换。
- 按两轨时间交集生成配对；没有两轨同时有效的区间不显示字幕，避免猜译文对应关系。
- 审查修正：显式ja等语言可匹配ja-orig；明确视频语言时不猜其他翻译轨；选择失败用ExtractorError阻止下载；视频替换失败恢复原ASS。
