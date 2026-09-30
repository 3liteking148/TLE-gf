"""The Codeforces rating bounds have exactly one definition.

``cf_common.RATING_MIN`` / ``cf_common.RATING_MAX`` is the single source of
truth (mirroring ``atcoder_api.RATING_MIN``/``RATING_MAX``). Consumers keep
their own local names so prompts, error strings and call sites stay stable,
but the values must never drift apart again.
"""
import pytest

from tle.util import codeforces_common as cf_common

from tle.cogs._codeforces_gitgud import _CF_RATING_MAX, _CF_RATING_MIN
from tle.cogs._duel_helpers import _DUEL_OFFICIAL_CUTOFF
from tle.cogs._lockout_helpers import LOWER_RATING, UPPER_RATING
from tle.cogs._training_helpers import (
    _TRAINING_MAX_RATING_VALUE,
    _TRAINING_MIN_RATING_VALUE,
)

_PAIR = (cf_common.RATING_MIN, cf_common.RATING_MAX)


def test_canonical_bounds():
    assert _PAIR == (800, 3500)


def test_gitgud_aliases():
    assert (_CF_RATING_MIN, _CF_RATING_MAX) == _PAIR


def test_training_aliases():
    assert (_TRAINING_MIN_RATING_VALUE,
            _TRAINING_MAX_RATING_VALUE) == _PAIR


def test_lockout_aliases():
    assert (LOWER_RATING, UPPER_RATING) == _PAIR


def test_duel_official_cutoff():
    assert _DUEL_OFFICIAL_CUTOFF == cf_common.RATING_MAX


def test_gitgud_error_text_tracks_bounds():
    from tle.cogs._codeforces_gitgud import _CfBackend
    from tle.cogs._codeforces_helpers import CodeforcesCogError

    expected = ('Wrong rating requested. Remember gitgud now uses rating '
                f'({cf_common.RATING_MIN}-{cf_common.RATING_MAX}) '
                'instead of delta.')
    with pytest.raises(CodeforcesCogError) as exc:
        _CfBackend().parse_args(['3600'], 1500)
    assert str(exc.value) == expected
