import re
import unittest

from toolbox.media.youtube.subtitles import build_bilingual_ass


def _events(value):
    return [line for line in value.splitlines() if line.startswith("Dialogue:")]


class BilingualAssTests(unittest.TestCase):
    def test_rolling_cues_keep_only_new_line_and_do_not_overlap(self):
        original = """WEBVTT

00:00:00.000 --> 00:00:03.000
A

00:00:01.000 --> 00:00:05.000
A
B

00:00:03.000 --> 00:00:07.000
B
C
"""
        chinese = """WEBVTT

00:00:00.000 --> 00:00:03.000
甲

00:00:01.000 --> 00:00:05.000
甲
乙

00:00:03.000 --> 00:00:07.000
乙
丙
"""
        events = _events(build_bilingual_ass(original, chinese))
        self.assertEqual(len(events), 3)
        self.assertIn(",0:00:00.00,0:00:01.00,", events[0])
        self.assertIn("A\\N甲", events[0])
        self.assertIn("B\\N乙", events[1])
        self.assertIn("C\\N丙", events[2])
        for first, second in zip(events, events[1:]):
            first_end = re.search(r"Dialogue: 0,[^,]+,([^,]+),", first).group(1)
            second_start = re.search(r"Dialogue: 0,([^,]+),", second).group(1)
            self.assertLessEqual(first_end, second_start)

    def test_rolling_cue_keeps_all_new_lines(self):
        original = """WEBVTT

00:00:00.000 --> 00:00:02.000
A

00:00:01.000 --> 00:00:03.000
A
B
C
"""
        chinese = """WEBVTT

00:00:00.000 --> 00:00:03.000
甲
"""
        events = _events(build_bilingual_ass(original, chinese))
        self.assertEqual(len(events), 2)
        self.assertIn("B C\\N甲", events[1])

    def test_repeated_rolling_snapshot_does_not_restore_old_line(self):
        original = """WEBVTT

00:00:00.000 --> 00:00:04.000
A

00:00:01.000 --> 00:00:04.000
A
B

00:00:02.000 --> 00:00:04.000
A
B

00:00:03.000 --> 00:00:05.000
B
C
"""
        chinese = """WEBVTT

00:00:00.000 --> 00:00:05.000
中
"""
        events = _events(build_bilingual_ass(original, chinese))
        self.assertEqual(len(events), 3)
        self.assertIn("A\\N中", events[0])
        self.assertIn("B\\N中", events[1])
        self.assertIn("C\\N中", events[2])

    def test_word_level_accumulation_is_fixed_to_one_line(self):
        original = """WEBVTT

00:00:00.000 --> 00:00:04.000
hello

00:00:01.000 --> 00:00:04.000
hello world
"""
        chinese = """WEBVTT

00:00:00.000 --> 00:00:04.000
你好
"""
        events = _events(build_bilingual_ass(original, chinese))
        self.assertEqual(len(events), 2)
        self.assertTrue(any("hello world\\N你好" in event for event in events))
        self.assertNotIn("hello\\N你好\\N", "\n".join(events))

    def test_identical_repetition_is_merged_but_real_repetition_is_kept(self):
        original = """WEBVTT

00:00:00.000 --> 00:00:01.000
go

00:00:00.500 --> 00:00:02.000
go

00:00:02.000 --> 00:00:03.000
go
"""
        chinese = """WEBVTT

00:00:00.000 --> 00:00:01.000
走

00:00:02.000 --> 00:00:03.000
走
"""
        events = _events(build_bilingual_ass(original, chinese))
        self.assertEqual(len(events), 2)
        self.assertIn(",0:00:00.00,0:00:01.00,", events[0])
        self.assertIn(",0:00:02.00,0:00:03.00,", events[1])

    def test_adjacent_identical_cues_can_be_a_real_repetition(self):
        original = """WEBVTT

00:00:00.000 --> 00:00:01.000
again

00:00:01.000 --> 00:00:02.000
again
"""
        chinese = """WEBVTT

00:00:00.000 --> 00:00:02.000
再来
"""
        events = _events(build_bilingual_ass(original, chinese))
        self.assertEqual(len(events), 2)

    def test_tracks_are_paired_by_time_not_sequence_number(self):
        original = """WEBVTT

00:00:00.000 --> 00:00:02.000
one

00:00:02.000 --> 00:00:04.000
two
"""
        chinese = """WEBVTT

00:00:00.500 --> 00:00:01.500
一

00:00:01.500 --> 00:00:03.500
二

00:00:03.500 --> 00:00:04.000
三
"""
        events = _events(build_bilingual_ass(original, chinese))
        self.assertEqual(len(events), 4)
        self.assertIn("one\\N一", events[0])
        self.assertIn("one\\N二", events[1])
        self.assertIn("two\\N二", events[2])
        self.assertIn("two\\N三", events[3])

    def test_vtt_tags_entities_empty_cues_and_ass_escape(self):
        original = """WEBVTT

00:00:00.000 --> 00:00:01.000 align:start
<b>A &amp; B</b> {\\an8} \\N

00:00:01.000 --> 00:00:02.000

"""
        chinese = """WEBVTT

00:00:00.000 --> 00:00:02.000
中 {\"x\"}
"""
        result = build_bilingual_ass(original, chinese)
        event = _events(result)[0]
        self.assertIn("A & B ｛＼an8｝ ＼N\\N中 ｛\"x\"｝", event)
        self.assertNotIn("{\\an8}", event)

    def test_long_text_gets_smaller_font(self):
        original = """WEBVTT

00:00:00.000 --> 00:00:01.000
""" + ("long text " * 100) + "\n"
        chinese = """WEBVTT

00:00:00.000 --> 00:00:01.000
中文
"""
        event = _events(build_bilingual_ass(original, chinese))[0]
        self.assertIn(r"\fscx", event)
        scale = int(re.search(r"\\fscx(\d+)", event).group(1))
        self.assertLess(scale, 100)
        self.assertIn(r"\fs84", event)
        self.assertLessEqual(scale * 550, 1900)

    def test_short_chinese_only_uses_doubled_font_size(self):
        chinese = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n中文\n"
        result = build_bilingual_ass(None, chinese)
        self.assertIn("Style: Bilingual,Arial Unicode MS,84,", result)
        self.assertIn(r"{\fs84}中文", _events(result)[0])

    def test_empty_or_non_overlapping_tracks_fail(self):
        with self.assertRaises(ValueError):
            build_bilingual_ass("WEBVTT\n", "WEBVTT\n")
        with self.assertRaises(ValueError):
            build_bilingual_ass(
                "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\na\n",
                "WEBVTT\n\n00:00:02.000 --> 00:00:03.000\n甲\n",
            )

    def test_gap_does_not_remove_a_legitimate_repeated_line(self):
        original = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nA\n\n00:00:05.000 --> 00:00:06.000\nA\nB\n"
        chinese = "WEBVTT\n\n00:00:00.000 --> 00:00:06.000\n中文\n"
        events = _events(build_bilingual_ass(original, chinese))
        self.assertIn("A B\\N中文", events[-1])

    def test_sub_centisecond_cue_does_not_create_zero_duration_ass(self):
        original = "WEBVTT\n\n00:00:00.001 --> 00:00:00.002\nshort\n\n00:00:00.100 --> 00:00:01.000\nvalid\n"
        chinese = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n中文\n"
        events = _events(build_bilingual_ass(original, chinese))
        self.assertEqual(len(events), 1)
        self.assertIn(",0:00:00.10,0:00:01.00,", events[0])
        self.assertIn("valid\\N中文", events[0])


if __name__ == "__main__":
    unittest.main()
