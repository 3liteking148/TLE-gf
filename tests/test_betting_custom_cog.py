"""Cog-layer tests for user-created markets: `;bet open`/`pick`/`list`,
permissions, the shared thread lifecycle, the thread listener and `;bet me`."""
import asyncio
import time

import pytest  # noqa: F401

from tle import constants
from tle.util import codeforces_common as cf_common
from tle.util import discord_common
from tle.cogs.betting import Betting, BettingCogError
from tests.betting_test_utils import (  # noqa: F401
    GUILD, CH, THREAD, USER_A, USER_B, db, _make_market,
    _make_custom_market, _FakeChannel, _FakeGuild, _FakeBot, _FakeThread,
    _FakeBetMessage, _Ctx, _admin_role, _wager,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cog(db, monkeypatch):
    monkeypatch.setattr(cf_common, 'user_db', db)
    monkeypatch.setattr(constants, 'BET_START_BALANCE', 1000, raising=False)
    monkeypatch.setattr(constants, 'BET_MIN_STAKE', 1, raising=False)
    monkeypatch.setattr(discord_common, '_BOT_PREFIX', ';', raising=False)
    channel = _FakeChannel(int(CH))
    guild = _FakeGuild(int(GUILD), channel)
    bot = _FakeBot([guild], {int(CH): channel})
    return Betting(bot), db, channel


@pytest.fixture
def setup(cog, db):
    db.set_guild_config(GUILD, 'bet_custom', '1')
    return cog


class TestOpenCommand:
    def test_requires_feature_flag(self, setup):
        cog, db, channel = setup
        db.delete_guild_config(GUILD, 'bet_custom')
        with pytest.raises(BettingCogError):
            _run(cog._cmd_custom_open(_Ctx(channel, USER_A),
                                      'Rain? :: Yes | No'))

    def test_creates_market_thread_and_choices(self, setup):
        cog, db, channel = setup
        _run(cog._cmd_custom_open(_Ctx(channel, USER_A),
                                  'Will it rain? :: Yes | No :: 2h'))
        markets = db.bet_markets_open(GUILD, kind='custom')
        assert len(markets) == 1
        market = markets[0]
        assert market.title == 'Will it rain?'
        assert market.created_by == str(USER_A)
        assert market.closes_at is not None
        choices = db.bet_custom_market_choices(market.market_id)
        assert [c.label for c in choices] == ['Yes', 'No']
        assert market.message_id is not None and market.thread_id is not None
        thread = channel.sent[0].thread
        assert len(thread.sent) == 1  # intro embed posted
        assert market.thread_intro_id is not None

    def test_open_cap(self, setup):
        cog, db, channel = setup
        for i in range(5):
            # Different channels so the per-channel open guard doesn't fire
            # before the per-user cap is reached.
            _make_custom_market(db, title=f'Market {i}', channel=f'chan{i}')
        with pytest.raises(BettingCogError):
            _run(cog._cmd_custom_open(_Ctx(channel, USER_A), 'Rain? :: Yes | No'))

    def test_open_blocked_by_existing_market_in_channel(self, setup):
        cog, db, channel = setup
        _make_market(db, commence=time.time() + 3600)
        with pytest.raises(BettingCogError):
            _run(cog._cmd_custom_open(_Ctx(channel, USER_A), 'Rain? :: Yes | No'))


class TestKindGuards:
    """Soccer-only operations must refuse custom markets instead of silently
    no-op'ing, and admin place-for must dispatch through the resolver."""

    def test_setodds_refused_on_custom(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        with pytest.raises(BettingCogError):
            _run(cog._cmd_setodds(_Ctx(channel, USER_A), 2.0, 3.0, 4.0))
        assert db.bet_market_get(mid).odds_home is None

    def test_bet_not_refused_on_custom(self, setup):
        cog, db, channel = setup
        _make_custom_market(db)
        with pytest.raises(BettingCogError):
            _run(cog._cmd_bet_not(_Ctx(channel, USER_A), 'yes 100'))

    def test_fixed_outcome_refused_on_custom(self, setup):
        cog, db, channel = setup
        # Even a custom choice literally named "home" must not be reachable
        # through the soccer-only fixed-outcome command.
        _make_custom_market(db, choices=(('home', 'Home'), ('away', 'Away')))
        with pytest.raises(BettingCogError):
            _run(cog._place_outcome(_Ctx(channel, USER_A), 'home', '100'))

    def test_fixed_outcome_works_on_soccer(self, setup):
        cog, db, channel = setup
        mid = _make_market(db, commence=time.time() + 3600)
        _run(cog._place_outcome(_Ctx(channel, USER_A), 'home', '100'))
        assert db.bet_get_wager(mid, USER_A, 'home').stake == 100

    def test_place_for_works_on_custom(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        db.bet_market_set_thread(mid, THREAD)
        member = type('M', (), {'id': USER_B, 'bot': False,
                                'display_name': 'Bob'})()
        _run(cog._cmd_place_for(_Ctx(channel, USER_A), member, 'Yes 100'))
        assert db.bet_get_wager(mid, USER_B, 'yes').stake == 100
        assert db.bet_get_balance(GUILD, USER_B) == 900


class TestPickCommand:
    def test_pick_places_bet(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        db.bet_market_set_thread(mid, THREAD)
        _run(cog._cmd_pick(_Ctx(channel, USER_A), 'Yes 100'))
        assert db.bet_get_wager(mid, USER_A, 'yes').stake == 100
        assert db.bet_get_balance(GUILD, USER_A) == 900

    def test_pick_unknown_choice_rejected(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        with pytest.raises(BettingCogError):
            _run(cog._cmd_pick(_Ctx(channel, USER_A), 'Banana 100'))

    def test_pick_works_on_soccer_market(self, setup):
        cog, db, channel = setup
        _make_market(db, commence=time.time() + 3600)
        _run(cog._cmd_pick(_Ctx(channel, USER_A), 'Spain 100'))
        mid = db.bet_market_get_active(GUILD, CH).market_id
        assert db.bet_get_wager(mid, USER_A, 'home').stake == 100
        assert db.bet_get_balance(GUILD, USER_A) == 900
        for key in list(cog._pool_refresh_timers):
            cog._pool_refresh_timers[key].cancel()

    def test_thread_listener_places_custom_bet(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        db.bet_market_set_thread(mid, THREAD)
        msg = _FakeBetMessage('Yes 100', channel_id=int(THREAD))
        _run(cog.on_message(msg))
        assert db.bet_get_wager(mid, 1, 'yes').stake == 100
        assert '✅' in msg.reactions


class TestCustomRemovalDisabled:
    """Custom stakes are final: neither the inline `0` shorthand nor
    `;bet withdraw` may remove a custom wager."""

    def _user(self, user_id):
        return type('U', (), {'id': user_id})()

    def test_inline_zero_does_not_remove(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        db.bet_market_set_thread(mid, THREAD)
        _wager(db, mid, USER_A, 'yes')
        status, _ = _run(cog._execute_bet(
            GUILD, db.bet_market_get(mid), self._user(USER_A), 'yes', '0'))
        assert status == 'invalid'
        assert db.bet_get_wager(mid, USER_A, 'yes').stake == 100
        assert db.bet_get_balance(GUILD, USER_A) == 900

    def test_pick_zero_rejected(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        db.bet_market_set_thread(mid, THREAD)
        _wager(db, mid, USER_A, 'yes')
        with pytest.raises(BettingCogError):
            _run(cog._cmd_pick(_Ctx(channel, USER_A), 'Yes 0'))
        assert db.bet_get_wager(mid, USER_A, 'yes').stake == 100

    def test_withdraw_refused_on_custom(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        db.bet_market_set_thread(mid, THREAD)
        _wager(db, mid, USER_A, 'yes')
        with pytest.raises(BettingCogError):
            _run(cog._withdraw_match(_Ctx(channel, USER_A)))
        assert db.bet_get_wager(mid, USER_A, 'yes').stake == 100
        assert db.bet_get_balance(GUILD, USER_A) == 900


class TestSettleAndPermissions:
    def test_soccer_settle_requires_admin(self, cog):
        """Regression: settle/cancel/close must still be admin-gated for
        soccer markets even though the decorator moved into the impl."""
        market_cog, db, channel = cog
        _make_market(db, commence=time.time() + 3600)
        with pytest.raises(BettingCogError):
            _run(market_cog._cmd_settle(_Ctx(channel, USER_A), 'home'))
        with pytest.raises(BettingCogError):
            _run(market_cog._cmd_cancel(_Ctx(channel, USER_A)))
        with pytest.raises(BettingCogError):
            _run(market_cog._cmd_close(_Ctx(channel, USER_A)))
        assert db.bet_market_get_active(GUILD, CH).status == 'open'

    def test_only_creator_or_admin_settles(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db, created_by=USER_A)
        _wager(db, mid, USER_B, 'yes')
        with pytest.raises(BettingCogError):
            _run(cog._cmd_settle(_Ctx(channel, USER_B), 'yes'))
        _run(cog._cmd_settle(_Ctx(channel, USER_B, roles=[_admin_role()]), 'yes'))
        assert db.bet_market_get(mid).status == 'settled'

    def test_creator_settles_and_winners_paid(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db, created_by=USER_A)
        _wager(db, mid, USER_B, 'yes')
        _wager(db, mid, USER_A, 'no')
        db.bet_market_set_thread(mid, THREAD)
        _run(cog._cmd_settle(_Ctx(channel, USER_A), 'Yes'))
        assert db.bet_market_get(mid).status == 'settled'
        assert db.bet_get_balance(GUILD, USER_B) == 1100
        finals = [m for m in channel.sent
                  if m.embed is not None and 'final' in (m.embed.title or '')]
        assert finals

    def test_cancel_refunds_and_locks_thread(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        _wager(db, mid, USER_A, 'yes')
        db.bet_market_set_thread(mid, THREAD)
        thread = _FakeThread(int(THREAD))
        cog.bot._channels[int(THREAD)] = thread
        _run(cog._cmd_cancel(_Ctx(channel, USER_A)))
        assert db.bet_get_balance(GUILD, USER_A) == 1000
        assert db.bet_market_get(mid).status == 'cancelled'
        assert thread.archived and thread.locked

    def test_close_marks_betting_closed_and_edits_message(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        msg = _run(channel.send(embed=None))
        db.bet_market_set_message(mid, msg.id)
        _run(cog._cmd_close(_Ctx(channel, USER_A)))
        assert db.bet_market_get(mid).bets_closed == 1
        assert msg.edited_embed is not None
        assert 'Betting ended' in msg.edited_embed.description


class TestListAndMe:
    def test_list_shows_open_markets(self, setup):
        cog, db, channel = setup
        _make_custom_market(db)
        ctx = _Ctx(channel, USER_A)
        _run(cog._cmd_custom_list(ctx))
        embed = ctx.sent[0].embed
        assert 'Will it rain?' in embed.description
        assert 'Yes' in embed.description

    def test_me_includes_custom_active_bet(self, setup):
        cog, db, channel = setup
        mid = _make_custom_market(db)
        db.bet_market_set_thread(mid, THREAD)
        _wager(db, mid, USER_A, 'yes')
        ctx = _Ctx(channel, USER_A)
        _run(Betting.me.__wrapped__(cog, ctx))
        embed = ctx.sent[0].embed
        fields = {f['name']: f['value'] for f in embed.fields}
        assert 'Will it rain?' in fields['Active bets']
