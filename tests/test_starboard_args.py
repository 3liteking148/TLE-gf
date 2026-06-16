"""Tests for _parse_starboard_args — the argument parser shared by the
starboard leaderboard / top commands (emoji + timeline/date filters).
"""
from datetime import datetime

from tests.starboard_test_utils import STAR
from tle.cogs.starboard import _parse_starboard_args, _NO_TIME_BOUND


class TestParseStarboardArgs:
    """Test the argument parser for starboard leaderboard/top commands."""

    def test_no_args_defaults_to_star_all_time(self):
        emoji, dlo, dhi = _parse_starboard_args(())
        assert emoji == STAR
        assert dlo == 0
        assert dhi == _NO_TIME_BOUND

    def test_emoji_only(self):
        emoji, dlo, dhi = _parse_starboard_args(('\N{FIRE}',))
        assert emoji == '\N{FIRE}'
        assert dlo == 0
        assert dhi == _NO_TIME_BOUND

    def test_star_emoji_explicit(self):
        emoji, dlo, dhi = _parse_starboard_args(('\N{WHITE MEDIUM STAR}',))
        assert emoji == '\N{WHITE MEDIUM STAR}'
        assert dlo == 0
        assert dhi == _NO_TIME_BOUND

    def test_week_keyword_defaults_star(self):
        emoji, dlo, dhi = _parse_starboard_args(('week',))
        assert emoji == STAR
        assert dlo > 0
        assert dhi == _NO_TIME_BOUND

    def test_month_keyword_defaults_star(self):
        emoji, dlo, dhi = _parse_starboard_args(('month',))
        assert emoji == STAR
        assert dlo > 0
        assert dhi == _NO_TIME_BOUND

    def test_year_keyword_defaults_star(self):
        emoji, dlo, dhi = _parse_starboard_args(('year',))
        assert emoji == STAR
        assert dlo > 0
        assert dhi == _NO_TIME_BOUND

    def test_emoji_and_week(self):
        emoji, dlo, dhi = _parse_starboard_args(('\N{FIRE}', 'week'))
        assert emoji == '\N{FIRE}'
        assert dlo > 0

    def test_week_and_emoji_reversed_order(self):
        """Order shouldn't matter."""
        emoji, dlo, dhi = _parse_starboard_args(('week', '\N{FIRE}'))
        assert emoji == '\N{FIRE}'
        assert dlo > 0

    def test_week_sets_monday(self):
        emoji, dlo, dhi = _parse_starboard_args(('week',))
        monday = datetime.fromtimestamp(dlo)
        assert monday.weekday() == 0  # Monday
        assert monday.hour == 0
        assert monday.minute == 0
        assert monday.second == 0

    def test_month_sets_first_of_month(self):
        emoji, dlo, dhi = _parse_starboard_args(('month',))
        first = datetime.fromtimestamp(dlo)
        assert first.day == 1
        assert first.hour == 0
        assert first.minute == 0

    def test_year_sets_jan_first(self):
        emoji, dlo, dhi = _parse_starboard_args(('year',))
        jan1 = datetime.fromtimestamp(dlo)
        assert jan1.month == 1
        assert jan1.day == 1
        assert jan1.hour == 0

    def test_dge_date_arg(self):
        """d>=01012025 should set dlo to Jan 1 2025."""
        emoji, dlo, dhi = _parse_starboard_args(('d>=01012025',))
        assert emoji == STAR
        dt_obj = datetime.fromtimestamp(dlo)
        assert dt_obj.year == 2025
        assert dt_obj.month == 1
        assert dt_obj.day == 1

    def test_dlt_date_arg(self):
        """d<01022025 should set dhi to Feb 1 2025."""
        emoji, dlo, dhi = _parse_starboard_args(('d<01022025',))
        assert emoji == STAR
        dt_obj = datetime.fromtimestamp(dhi)
        assert dt_obj.year == 2025
        assert dt_obj.month == 2
        assert dt_obj.day == 1

    def test_dge_and_dlt_combined(self):
        emoji, dlo, dhi = _parse_starboard_args(('d>=01012025', 'd<01022025'))
        assert emoji == STAR
        assert dlo < dhi
        lo_dt = datetime.fromtimestamp(dlo)
        hi_dt = datetime.fromtimestamp(dhi)
        assert lo_dt.month == 1
        assert hi_dt.month == 2

    def test_emoji_with_dge_and_dlt(self):
        emoji, dlo, dhi = _parse_starboard_args(('\N{FIRE}', 'd>=01012025', 'd<01022025'))
        assert emoji == '\N{FIRE}'
        assert dlo > 0
        assert dhi < _NO_TIME_BOUND

    def test_year_only_format(self):
        """d>=2024 should parse as Jan 1 2024."""
        emoji, dlo, dhi = _parse_starboard_args(('d>=2024',))
        dt_obj = datetime.fromtimestamp(dlo)
        assert dt_obj.year == 2024
        assert dt_obj.month == 1
        assert dt_obj.day == 1

    def test_month_year_format(self):
        """d>=032025 should parse as March 2025."""
        emoji, dlo, dhi = _parse_starboard_args(('d>=032025',))
        dt_obj = datetime.fromtimestamp(dlo)
        assert dt_obj.year == 2025
        assert dt_obj.month == 3

    def test_keyword_case_insensitive(self):
        emoji, dlo, dhi = _parse_starboard_args(('Week',))
        assert emoji == STAR
        assert dlo > 0

    def test_keyword_uppercase(self):
        emoji, dlo, dhi = _parse_starboard_args(('MONTH',))
        assert emoji == STAR
        assert dlo > 0

    def test_timeline_keyword_not_treated_as_emoji(self):
        """'week' should not be stored as the emoji."""
        emoji, dlo, dhi = _parse_starboard_args(('week',))
        assert emoji != 'week'
        assert emoji == STAR

    def test_multiple_emojis_last_wins(self):
        """If multiple non-keyword args given, last one is the emoji."""
        emoji, dlo, dhi = _parse_starboard_args(('\N{FIRE}', '\N{HEAVY BLACK HEART}'))
        assert emoji == '\N{HEAVY BLACK HEART}'

    def test_week_dge_combined_uses_max_dlo(self):
        """d>= should take max with week's dlo."""
        # Use a date far in the future to ensure it overrides week
        emoji, dlo, dhi = _parse_starboard_args(('week', 'd>=01012030'))
        dt_obj = datetime.fromtimestamp(dlo)
        assert dt_obj.year == 2030

    def test_default_emoji_override(self):
        emoji, dlo, dhi = _parse_starboard_args(('week',), default_emoji='\N{FIRE}')
        assert emoji == '\N{FIRE}'
