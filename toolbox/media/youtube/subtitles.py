"""Utilities for turning YouTube's rolling captions into fixed bilingual ASS."""

from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Iterable


_TIMING_RE = re.compile(
    r"^\s*(?P<start>(?:\d+:)?\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>(?:\d+:)?\d{2}:\d{2}\.\d{3})(?:\s+.*)?$"
)
_TAG_RE = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class _Cue:
    start: float
    end: float
    lines: tuple[str, ...]


@dataclass(frozen=True)
class _Segment:
    start: float
    end: float
    text: str


def _parse_timestamp(value: str) -> float:
    parts = value.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        hours = 0
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"invalid WebVTT timestamp: {value!r}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _clean_line(line: str) -> str:
    # YouTube uses timestamp tags for word-level timing.  ASS will provide one
    # stable event, so those tags must not survive as literal markup.
    line = html.unescape(_TAG_RE.sub("", line))
    return re.sub(r"\s+", " ", line).strip()


def _parse_vtt(value: str) -> list[_Cue]:
    """Parse ordinary WebVTT cues while ignoring metadata blocks."""
    if not isinstance(value, str):
        raise TypeError("subtitle content must be text")

    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[_Cue] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line == "WEBVTT":
            index += 1
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")) and (
                line == "NOTE" or line.startswith("NOTE ") or line in {"STYLE", "REGION"}):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue

        # A cue may have an identifier immediately before its timing line.
        timing_index = index
        match = _TIMING_RE.match(lines[timing_index])
        if match is None and timing_index + 1 < len(lines):
            match = _TIMING_RE.match(lines[timing_index + 1])
            if match is not None:
                timing_index += 1
        if match is None:
            index += 1
            continue

        payload: list[str] = []
        index = timing_index + 1
        while index < len(lines) and lines[index].strip():
            cleaned = _clean_line(lines[index])
            if cleaned:
                payload.append(cleaned)
            index += 1
        start = _parse_timestamp(match.group("start"))
        end = _parse_timestamp(match.group("end"))
        if end > start and payload:
            cues.append(_Cue(start, end, tuple(payload)))
    return cues


def _rolling_suffix(lines: tuple[str, ...], previous: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return newly introduced lines when two rolling cues overlap."""
    if len(lines) < 2 or not previous:
        return None
    if lines == previous:
        return ()
    # A -> A/B/C: retain both newly introduced lines rather than dropping C.
    if len(lines) > len(previous) and lines[:len(previous)] == previous:
        return lines[len(previous):]
    # A/B -> B/C: retain C after the maximal suffix/prefix overlap (B).
    max_overlap = min(len(previous), len(lines) - 1)
    for overlap in range(max_overlap, 0, -1):
        if previous[-overlap:] == lines[:overlap]:
            return lines[overlap:]
    return None


def _looks_rolling(lines: tuple[str, ...], previous: tuple[str, ...],
                   previous_end: float | None, start: float) -> tuple[str, ...] | None:
    if previous_end is None or start > previous_end + 0.05:
        return None
    return _rolling_suffix(lines, previous)


def _normalise_track(cues: Iterable[_Cue]) -> list[_Segment]:
    """Collapse rolling cues into non-overlapping, one-line display segments.

    This deliberately does not guess sentence boundaries.  For word-by-word
    captions each updated cue becomes a short, non-overlapping event; for
    rolling multi-line captions only the newly introduced bottom line remains.
    """
    ordered = sorted(cues, key=lambda cue: (cue.start, cue.end))
    segments: list[_Segment] = []
    previous_lines: tuple[str, ...] = ()
    previous_end: float | None = None
    previous_text: str | None = None
    for cue in ordered:
        lines = tuple(line for line in cue.lines if line)
        if not lines:
            continue
        previous_cue_end = previous_end
        rolling_suffix = _looks_rolling(lines, previous_lines, previous_end, cue.start)
        if rolling_suffix is not None:
            text = previous_text if not rolling_suffix and previous_text is not None else " ".join(rolling_suffix)
        else:
            text = " ".join(lines)
        previous_lines = lines
        previous_end = cue.end
        previous_text = text
        if not text:
            continue
        if segments:
            previous = segments[-1]
            clipped_end = min(previous.end, cue.start)
            if clipped_end <= previous.start:
                segments.pop()
            elif clipped_end != previous.end:
                segments[-1] = _Segment(previous.start, clipped_end, previous.text)
        if cue.end <= cue.start:
            continue
        candidate = _Segment(cue.start, cue.end, text)
        if (segments and segments[-1].text == candidate.text
                and abs(segments[-1].end - candidate.start) < 0.01
                and previous_cue_end is not None and cue.start < previous_cue_end - 1e-6):
            previous = segments[-1]
            segments[-1] = _Segment(previous.start, candidate.end, previous.text)
        else:
            segments.append(candidate)
    return [segment for segment in segments if segment.end > segment.start]


def _pair_segments(original: list[_Segment], chinese: list[_Segment]) -> list[tuple[float, float, str, str]]:
    if not original or not chinese:
        raise ValueError("both original and Chinese subtitles must contain cues")
    paired: list[tuple[float, float, str, str]] = []
    original_index = chinese_index = 0
    while original_index < len(original) and chinese_index < len(chinese):
        original_active = original[original_index]
        chinese_active = chinese[chinese_index]
        start = max(original_active.start, chinese_active.start)
        end = min(original_active.end, chinese_active.end)
        if end > start:
            pair = (start, end, original_active.text, chinese_active.text)
        else:
            pair = None
        if pair is not None:
            start_cs = int(round(start * 100))
            end_cs = int(round(end * 100))
            if end_cs > start_cs:
                # Keep adjacent identical pairs as separate events: a speaker
                # may intentionally repeat a short sentence.
                paired.append(pair)
        if original_active.end <= chinese_active.end:
            original_index += 1
        if chinese_active.end <= original_active.end:
            chinese_index += 1
    if not paired:
        raise ValueError("original and Chinese subtitles have no overlapping cues")
    return paired


def _ass_escape(text: str) -> str:
    # Escape user text before adding our own ASS line break.  Braces and
    # backslashes are the only characters that can turn caption text into an
    # override command in libass.
    # ASS has no portable literal-backslash escape: a second backslash can
    # still begin a command in some renderers.  Full-width punctuation keeps
    # the spoken text visible while making command injection impossible.
    text = text.replace("\\", "＼")
    text = text.replace("{", "｛").replace("}", "｝")
    return text.replace("\r", "").replace("\n", " ")


def _font_style(original: str, chinese: str) -> str:
    def width(value: str) -> float:
        return sum(1.0 if ord(char) > 0x2FF else 0.55 for char in value)

    longest = max(width(original), width(chinese), 1.0)
    if longest <= 19.0:
        return r"{\fs84}"
    scale = max(1, min(100, int(1900 / longest)))
    return f"{{\\fs84\\fscx{scale}}}"


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, int(round(seconds * 100)))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds_value, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds_value:02d}.{centiseconds:02d}"


def build_bilingual_ass(original_vtt: str | None, chinese_vtt: str) -> str:
    """Return fixed two-line ASS: original on top, Chinese below.

    The source VTTs are never modified.  Cue timing is intersected by time,
    rather than paired by cue number, because translated tracks are commonly
    segmented differently.
    """
    chinese = _normalise_track(_parse_vtt(chinese_vtt))
    if original_vtt is not None:
        original = _normalise_track(_parse_vtt(original_vtt))
        pairs = _pair_segments(original, chinese)
    else:
        if not chinese:
            raise ValueError("Chinese subtitles must contain cues")
        pairs = [(segment.start, segment.end, "", segment.text) for segment in chinese
                 if round(segment.end * 100) > round(segment.start * 100)]
        if not pairs:
            raise ValueError("Chinese subtitles must contain displayable cues")

    output = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Bilingual,Arial Unicode MS,84,&H00FFFFFF,&H00FFFFFF,&H80000000,&H80000000,0,0,0,0,100,100,0,0,3,2,0,2,50,50,45,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for start, end, original_text, chinese_text in pairs:
        style = _font_style(original_text, chinese_text)
        text = f"{style}{_ass_escape(original_text)}\\N{_ass_escape(chinese_text)}" if original_text else f"{style}{_ass_escape(chinese_text)}"
        output.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Bilingual,,0,0,0,,{text}"
        )
    return "\n".join(output) + "\n"


__all__ = ["build_bilingual_ass"]
