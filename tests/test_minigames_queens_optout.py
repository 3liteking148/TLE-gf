"""Queens sticky self opt-out: ``;queens unregister`` hides a user from every
ranking and keeps their data, and only the user themselves can rejoin via
``;queens register``."""
import asyncio
from types import SimpleNamespace

import pytest

from tle import constants
from tle.util import codeforces_common as cf_common
from tle.cogs._minigame_queens import QUEENS_GAME, normalize_queens_name
from tle.cogs.minigames import Minigames, MinigameCogError

from tests.minigames_test_utils import (
    _queens_number, db, _FakeGuild, _FakeDiscordMember, _QueensCommandsBase,
)


_NORM_ALICE = normalize_queens_name('Alice LinkedIn')
_NORM_BOB = normalize_queens_name('Bob LinkedIn')


class TestQueensOptOutDb:
    def test_optout_is_sticky_and_only_user_clears_it(self, db):
        assert db.is_minigame_opted_out(100, 'queens', '300') is False
        assert db.optout_minigame_user(100, 'queens', '300', 1.0) == 1
        # Idempotent: a second opt-out keeps the original row.
        assert db.optout_minigame_user(100, 'queens', '300', 2.0) == 0
        assert db.is_minigame_opted_out(100, 'queens', '300') is True
        assert {row.user_id for row in db.get_minigame_optouts(100, 'queens')} == {
            '300'}

        assert db.clear_minigame_optout(100, 'queens', '300') == 1
        assert db.is_minigame_opted_out(100, 'queens', '300') is False
        assert db.clear_minigame_optout(100, 'queens', '300') == 0

    def test_optout_is_scoped_per_guild_and_game(self, db):
        db.optout_minigame_user(100, 'queens', '300', 1.0)
        assert db.is_minigame_opted_out(100, 'queens', '300') is True
        assert db.is_minigame_opted_out(101, 'queens', '300') is False
        assert db.is_minigame_opted_out(100, 'akari', '300') is False


class TestQueensOptOut(_QueensCommandsBase):
    def _seed_two_players(self, db, cog):
        """Two registered players, each with a stored source result + rating."""
        db.set_guild_config(100, 'queens', '1')
        num = _queens_number('2026-06-08')
        db.save_minigame_unresolved_result(
            100, 'queens', _NORM_ALICE, 'Alice LinkedIn', 200, num,
            '2026-06-08', 100, 5, 1, 'raw')
        db.save_minigame_unresolved_result(
            100, 'queens', _NORM_BOB, 'Bob LinkedIn', 200, num,
            '2026-06-08', 100, 6, 1, 'raw')
        db.set_minigame_player_link(
            100, 'queens', 300, 'Alice LinkedIn', _NORM_ALICE, None, 1.0, 300)
        db.set_minigame_player_link(
            100, 'queens', 301, 'Bob LinkedIn', _NORM_BOB, None, 1.0, 301)
        cog._sync_queens_materialized_results(100)
        cog._recompute_minigame_ratings(100, QUEENS_GAME)

    @staticmethod
    def _rated_ids(db):
        return {row.user_id for row in db.get_minigame_ratings(100, 'queens')}

    def test_unregister_hides_keeps_data_then_self_register_restores(
            self, db, monkeypatch):
        monkeypatch.setattr(cf_common, 'user_db', db)
        alice = _FakeDiscordMember(300, 'alice', 'Alice')
        bob = _FakeDiscordMember(301, 'bob', 'Bob')
        guild = _FakeGuild(100, members=[alice, bob])
        cog = Minigames(bot=None)
        self._seed_two_players(db, cog)
        assert self._rated_ids(db) == {'300', '301'}

        ctx = self._make_ctx(guild, alice)
        asyncio.run(Minigames.queens_unregister.__wrapped__(cog, ctx, None))

        # Link gone, sticky opt-out set, hidden from ratings...
        assert db.get_minigame_player_link(100, 'queens', alice.id) is None
        assert db.is_minigame_opted_out(100, 'queens', alice.id) is True
        assert self._rated_ids(db) == {'301'}
        # ...but the stored source data is preserved.
        assert db.get_minigame_unresolved_results_for_name(
            100, 'queens', _NORM_ALICE)

        # The user re-registers themselves: opt-out lifts and data comes back.
        asyncio.run(cog._cmd_queens_set(ctx, alice, 'Alice LinkedIn'))
        assert db.is_minigame_opted_out(100, 'queens', alice.id) is False
        assert db.get_minigame_player_link(100, 'queens', alice.id) is not None
        cog._recompute_minigame_ratings(100, QUEENS_GAME)
        assert self._rated_ids(db) == {'300', '301'}

    def test_optout_filters_ratings_even_with_stale_link(self, db, monkeypatch):
        monkeypatch.setattr(cf_common, 'user_db', db)
        cog = Minigames(bot=None)
        self._seed_two_players(db, cog)

        # Opt out while a link still exists (simulates an import/forced re-link).
        db.optout_minigame_user(100, 'queens', 300, 1.0)
        cog._recompute_minigame_ratings(100, QUEENS_GAME)
        assert self._rated_ids(db) == {'301'}

        rows = db.get_minigame_results_for_guild(100, 'queens')
        kept = cog._filter_minigame_banned_rows(100, QUEENS_GAME, rows)
        assert all(row.user_id != '300' for row in kept)

    def test_others_cannot_reregister_opted_out_user(self, db, monkeypatch):
        monkeypatch.setattr(cf_common, 'user_db', db)
        db.set_guild_config(100, 'queens', '1')
        alice = _FakeDiscordMember(300, 'alice', 'Alice')
        mod = _FakeDiscordMember(
            999, 'mod', 'Mod',
            roles=[SimpleNamespace(name=constants.TLE_MODERATOR)])
        guild = _FakeGuild(100, members=[alice, mod])
        cog = Minigames(bot=None)
        db.optout_minigame_user(100, 'queens', alice.id, 1.0)

        ctx_mod = self._make_ctx(guild, mod)
        with pytest.raises(MinigameCogError, match='opted out'):
            asyncio.run(cog._cmd_queens_set(ctx_mod, alice, 'Alice LinkedIn'))

        assert db.get_minigame_player_link(100, 'queens', alice.id) is None
        assert db.is_minigame_opted_out(100, 'queens', alice.id) is True

    def test_self_register_lifts_optout(self, db, monkeypatch):
        monkeypatch.setattr(cf_common, 'user_db', db)
        db.set_guild_config(100, 'queens', '1')
        alice = _FakeDiscordMember(300, 'alice', 'Alice')
        guild = _FakeGuild(100, members=[alice])
        cog = Minigames(bot=None)
        db.optout_minigame_user(100, 'queens', alice.id, 1.0)

        ctx = self._make_ctx(guild, alice)
        asyncio.run(cog._cmd_queens_set(ctx, alice, 'Alice LinkedIn'))

        assert db.is_minigame_opted_out(100, 'queens', alice.id) is False
        assert db.get_minigame_player_link(100, 'queens', alice.id) is not None

    def test_sync_skips_opted_out_user(self, db, monkeypatch):
        monkeypatch.setattr(cf_common, 'user_db', db)
        db.set_guild_config(100, 'queens', '1')
        num = _queens_number('2026-06-08')
        db.save_minigame_unresolved_result(
            100, 'queens', _NORM_ALICE, 'Alice LinkedIn', 200, num,
            '2026-06-08', 100, 5, 1, 'raw')
        db.set_minigame_player_link(
            100, 'queens', 300, 'Alice LinkedIn', _NORM_ALICE, None, 1.0, 300)
        db.optout_minigame_user(100, 'queens', 300, 1.0)

        cog = Minigames(bot=None)
        cog._sync_queens_materialized_results(100)

        # No materialized result rows were produced for the opted-out user.
        rows = db.get_minigame_results_for_guild(100, 'queens')
        assert all(row.user_id != '300' for row in rows)
