"""Tests for the _apply_vc_deltas helper in tle/cogs/contests.py.

Regression: under CF's May 2026 contest.standings restriction, the API
returns CONTESTANT-only rows. The old _watch_rated_vc logic treated
every handle whose delta_by_handle entry was missing as 'did not
participate' and called remove_last_ratedvc_participation on them —
silently wiping all VC participations of every rated-VC member.

The helper must refuse to apply rating changes (return None) when no
VIRTUAL rows are present, so the caller can finish the VC cleanly
without destroying participation history.
"""
import importlib.util
import os
import sys
import types

import pytest


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_apply_helper():
    """Load tle.cogs.contests directly and return the _apply_vc_deltas
    helper. Heavy deps (numpy/discord.ui) are stubbed minimally."""
    # Use conftest's stubs by importing test infra first; the real
    # contests cog drags in tle.util.ranklist which needs numpy.fft —
    # stub it just enough.
    if 'tle.util.ranklist' not in sys.modules or not hasattr(
            sys.modules['tle.util.ranklist'], 'RanklistError'):
        rl = types.ModuleType('tle.util.ranklist')
        rl.RanklistError = type('RanklistError', (Exception,), {})

        class _Ranklist:
            pass
        rl.Ranklist = _Ranklist
        rl.HandleNotPresentError = type('HandleNotPresentError', (Exception,), {})
        rl.__path__ = []
        sys.modules['tle.util.ranklist'] = rl
        sys.modules['tle.util'].ranklist = rl

    from tle.util import tasks, events
    if not hasattr(tasks.Waiter, 'for_event'):
        tasks.Waiter.for_event = staticmethod(lambda ev: tasks.Waiter())
    for event_name in ('ContestListRefresh', 'RatingChangesUpdate'):
        if not hasattr(events, event_name):
            setattr(events, event_name, type(event_name, (), {}))

    from tle.util import cache_system2 as cs
    if not hasattr(cs, 'CacheError'):
        cs.CacheError = type('CacheError', (Exception,), {})
    if not hasattr(cs, 'RanklistNotMonitored'):
        cs.RanklistNotMonitored = type('RanklistNotMonitored', (Exception,), {})

    cogs_path = os.path.join(_ROOT, 'tle', 'cogs', 'contests.py')
    if '_contests_for_vc_test' in sys.modules:
        return sys.modules['_contests_for_vc_test']._apply_vc_deltas
    spec = importlib.util.spec_from_file_location('_contests_for_vc_test', cogs_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['_contests_for_vc_test'] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        del sys.modules['_contests_for_vc_test']
        pytest.skip(f'Could not load contests cog: {e}')
    return mod._apply_vc_deltas


# ── Helpers to build minimal stand-ins ────────────────────────────────

class _FakePartyType:
    def __init__(self, participantType):
        self.participantType = participantType


class _FakeRow:
    def __init__(self, participantType):
        self.party = _FakePartyType(participantType)


class _FakeRanklist:
    def __init__(self, standings, delta_by_handle):
        self.standings = standings
        self.delta_by_handle = delta_by_handle


class _FakeDb:
    def __init__(self, vc_ratings):
        self.vc_ratings = dict(vc_ratings)
        self.removed = []        # member_ids of removed participations
        self.updates = []        # (vc_id, member_id, new_rating) tuples

    def remove_last_ratedvc_participation(self, member_id):
        self.removed.append(member_id)

    def get_vc_rating(self, member_id):
        return self.vc_ratings.get(member_id, 1500)

    def update_vc_rating(self, vc_id, member_id, rating):
        self.updates.append((vc_id, member_id, rating))


# ── Tests ─────────────────────────────────────────────────────────────

class TestApplyVcDeltas:
    @pytest.fixture(autouse=True)
    def _apply(self):
        self._apply_vc_deltas = _load_apply_helper()

    def test_returns_none_when_no_virtual_rows(self):
        """CF restricted API returns CONTESTANT-only rows. Helper must
        refuse to touch the DB so VC history is preserved."""
        ranklist = _FakeRanklist(
            standings=[_FakeRow('CONTESTANT'), _FakeRow('CONTESTANT')],
            delta_by_handle={},
        )
        db = _FakeDb(vc_ratings={'1': 1500, '2': 1600})

        result = self._apply_vc_deltas(
            db, vc_id=42, handles=['alice', 'bob'],
            member_ids=['1', '2'], ranklist=ranklist)

        assert result is None
        assert db.removed == [], 'No participations should be removed'
        assert db.updates == [], 'No VC ratings should be updated'

    def test_applies_deltas_when_virtual_rows_present(self):
        ranklist = _FakeRanklist(
            standings=[_FakeRow('CONTESTANT'), _FakeRow('VIRTUAL'), _FakeRow('VIRTUAL')],
            delta_by_handle={'alice': 25, 'bob': -10},
        )
        db = _FakeDb(vc_ratings={'1': 1500, '2': 1600})

        result = self._apply_vc_deltas(
            db, vc_id=42, handles=['alice', 'bob'],
            member_ids=['1', '2'], ranklist=ranklist)

        assert result is not None
        assert set(result.keys()) == {'alice', 'bob'}
        assert result['alice'].oldRating == 1500
        assert result['alice'].newRating == 1525
        assert result['bob'].oldRating == 1600
        assert result['bob'].newRating == 1590
        assert db.removed == []
        assert sorted(db.updates) == [(42, '1', 1525), (42, '2', 1590)]

    def test_removes_non_participating_handle(self):
        """When VIRTUAL rows exist but a specific handle has no delta,
        that one user is treated as 'did not participate' and removed —
        existing behavior, must not regress."""
        ranklist = _FakeRanklist(
            standings=[_FakeRow('VIRTUAL'), _FakeRow('VIRTUAL')],
            delta_by_handle={'alice': 25},  # bob missing
        )
        db = _FakeDb(vc_ratings={'1': 1500, '2': 1600})

        result = self._apply_vc_deltas(
            db, vc_id=42, handles=['alice', 'bob'],
            member_ids=['1', '2'], ranklist=ranklist)

        assert set(result.keys()) == {'alice'}
        assert db.removed == ['2']
        assert db.updates == [(42, '1', 1525)]

    def test_empty_handles_list(self):
        """Edge case: no participants. Should return an empty dict (not None)
        as long as VIRTUAL rows exist."""
        ranklist = _FakeRanklist(
            standings=[_FakeRow('VIRTUAL')],
            delta_by_handle={},
        )
        db = _FakeDb(vc_ratings={})

        result = self._apply_vc_deltas(
            db, vc_id=42, handles=[], member_ids=[], ranklist=ranklist)

        assert result == {}
        assert db.removed == []
        assert db.updates == []
