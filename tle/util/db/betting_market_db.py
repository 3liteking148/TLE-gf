"""Generic betting market / wager access over the shared schema.

The archived soccer game and user-created custom markets share one ``bet_market``
base and one ``bet_wager`` table; each kind keeps its own columns in an extension
(``bet_market_soccer`` / ``bet_market_custom``). A read joins **both** extensions,
so a returned row always carries ``kind`` plus both kinds' columns and callers
need no per-kind table parameter. Kind-specific behaviour (pick labels, odds,
payout estimates, ownership, archive policy) lives in a per-kind backend
selected by ``_backend(market)`` — the only place that branches on ``kind``;
there is no per-market store object. Soccer settlement lives in
``BettingWagerDbMixin``; custom markets in ``BettingCustomDbMixin``.
"""
import sqlite3
import time

_SELECT = (
    'SELECT m.*, s.sport_key, s.home_team, s.away_team, s.odds_home, '
    's.odds_draw, s.odds_away, s.result_home, s.result_away, c.title '
    'FROM bet_market m '
    'LEFT JOIN bet_market_soccer s ON s.market_id = m.market_id '
    'LEFT JOIN bet_market_custom c ON c.market_id = m.market_id '
)


class _SoccerBackend:
    """Kind behaviour for the archived fixed-odds soccer game."""

    kind = 'soccer'
    supports_fixed_odds = True

    def title(self, db, market):
        return f'{market.home_team} vs {market.away_team}'

    def allows_draw(self, db, market):
        return (market.odds_draw or 0) > 1

    def pick_odds(self, db, market, pick):
        from tle.util.db.user_db_conn import _bet_odds_for_pick
        odds = {'home': market.odds_home, 'draw': market.odds_draw,
                'away': market.odds_away}
        return _bet_odds_for_pick(odds, pick)

    def pick_label(self, db, market, pick):
        from tle.util.db.user_db_conn import _bet_pick_base
        base = _bet_pick_base(pick)
        label = {'home': market.home_team, 'draw': 'Draw',
                 'away': market.away_team}[base]
        return f'Not {label}' if pick != base else label

    def pick_allowed(self, db, market, pick):
        from tle.util.db.user_db_conn import _bet_pick_base
        base = _bet_pick_base(pick)
        if base == 'draw':
            if not db.bet_market_allows_draw(market):
                return False
        elif base not in ('home', 'away'):
            return False
        # A not-pick on an odds<=1 outcome can never pay, so it isn't a bet.
        return self.pick_odds(db, market, pick) is not None

    def picks(self, db, market):
        order = ['home']
        if db.bet_market_allows_draw(market):
            order.append('draw')
        order.append('away')
        return order + [f'not_{pick}' for pick in order]

    def pick_potential(self, db, market, pick, stake):
        odds = self.pick_odds(db, market, pick)
        return int(round(stake * odds)) if odds is not None else 0

    def owned_by(self, market, user_id):
        return False


class _CustomBackend:
    """Kind behaviour for user-created parimutuel markets."""

    kind = 'custom'
    supports_fixed_odds = False

    def title(self, db, market):
        return market.title

    def allows_draw(self, db, market):
        return False

    def pick_odds(self, db, market, pick):
        total, totals = db.bet_market_pool_totals(market.market_id)
        return parimutuel_odds(total, totals.get(pick, 0))

    def pick_label(self, db, market, pick):
        row = db.bet_custom_market_choice(market.market_id, pick)
        return row.label if row is not None else str(pick)

    def pick_allowed(self, db, market, pick):
        return db.bet_custom_market_choice(market.market_id, pick) is not None

    def picks(self, db, market):
        return [r.choice_key
                for r in db.bet_custom_market_choices(market.market_id)]

    def pick_potential(self, db, market, pick, stake):
        total, totals = db.bet_market_pool_totals(market.market_id)
        staked = totals.get(pick, 0)
        return (stake * total) // staked if staked > 0 else None

    def owned_by(self, market, user_id):
        return str(market.created_by) == str(user_id)


_BACKENDS = {backend.kind: backend
             for backend in (_SoccerBackend(), _CustomBackend())}


def _backend(market):
    """The behaviour backend for a market's kind — the single place that
    branches on ``market.kind``."""
    return _BACKENDS[market.kind]


def parimutuel_odds(total_pool, staked):
    """Implied odds for one parimutuel choice: ``total_pool / staked``, or None
    when nobody has backed it yet. The one place this formula lives."""
    return (total_pool / staked) if staked > 0 else None


class BettingMarketDbMixin:
    """Mixin providing generic market access, pick math and shared stats."""

    # -- Generic reads --

    def _market_one(self, where, params, order=''):
        return self.conn.execute(
            f'{_SELECT}WHERE {where}{order}', params).fetchone()

    def _market_all(self, where, params, order=''):
        return self.conn.execute(
            f'{_SELECT}WHERE {where}{order}', params).fetchall()

    def bet_market_get(self, market_id):
        """Return a market by id, or None."""
        return self._market_one('m.market_id = ?', (market_id,))

    def bet_markets_open(self, guild_id, kind='soccer'):
        """Open markets for a guild, soonest-closing first. ``kind=None`` spans
        every market kind; the default is the archived soccer game."""
        if kind is None:
            return self._market_all(
                "m.guild_id = ? AND m.status = 'open'", (str(guild_id),),
                ' ORDER BY m.closes_at ASC, m.market_id ASC')
        return self._market_all(
            "m.guild_id = ? AND m.status = 'open' AND m.kind = ?",
            (str(guild_id), kind),
            ' ORDER BY m.closes_at ASC, m.market_id ASC')

    def bet_market_get_active(self, guild_id, channel_id, kind='soccer'):
        """Newest open market in a channel, or None. ``kind=None`` spans every
        kind."""
        where = "m.guild_id = ? AND m.channel_id = ? AND m.status = 'open'"
        params = (str(guild_id), str(channel_id))
        if kind is not None:
            where += ' AND m.kind = ?'
            params += (kind,)
        return self._market_one(
            where, params,
            ' ORDER BY m.created_at DESC, m.market_id DESC LIMIT 1')

    def bet_market_get_active_by_thread(self, guild_id, thread_id,
                                        kind='soccer'):
        """The open market whose betting thread is thread_id, or None."""
        where = "m.guild_id = ? AND m.thread_id = ? AND m.status = 'open'"
        params = (str(guild_id), str(thread_id))
        if kind is not None:
            where += ' AND m.kind = ?'
            params += (kind,)
        return self._market_one(where, params, ' LIMIT 1')

    def bet_markets_pending_lock(self):
        """Settled markets with an unlocked thread, across all guilds and both
        kinds — the delayed 12h thread-lock work-list."""
        return self._market_all(
            "m.status = 'settled' AND m.thread_id IS NOT NULL "
            'AND m.thread_locked = 0', (),
            ' ORDER BY m.settled_at ASC, m.market_id ASC')

    def bet_markets_pending_settlement(self, before_time):
        """Open soccer markets whose kickoff is at/before before_time — the
        auto-settle poller's work-list, across all guilds."""
        return self._market_all(
            "m.status = 'open' AND m.kind = 'soccer' AND m.closes_at <= ?",
            (before_time,), ' ORDER BY m.closes_at ASC')

    def bet_market_get_open_for_event(self, guild_id, event_id):
        """Return the guild's open soccer market on this event, or None."""
        return self._market_one(
            "m.guild_id = ? AND m.event_id = ? AND m.status = 'open' "
            "AND m.kind = 'soccer'",
            (str(guild_id), str(event_id)),
            ' ORDER BY m.market_id DESC LIMIT 1')

    def bet_market_get_open_for_fixture(self, guild_id, fixture_key):
        """Return the guild's open soccer market for a canonical fixture, or
        None."""
        return self._market_one(
            "m.guild_id = ? AND m.fixture_key = ? AND m.status = 'open' "
            "AND m.kind = 'soccer'",
            (str(guild_id), fixture_key),
            ' ORDER BY m.market_id DESC LIMIT 1')

    def bet_market_get_latest_settled_by_thread(self, guild_id, thread_id):
        """Most recently settled soccer market for a thread, or None."""
        return self._market_one(
            "m.guild_id = ? AND m.thread_id = ? AND m.status = 'settled' "
            "AND m.kind = 'soccer'",
            (str(guild_id), str(thread_id)),
            ' ORDER BY m.settled_at DESC, m.market_id DESC LIMIT 1')

    def bet_market_get_latest_settled_by_channel(self, guild_id, channel_id):
        """Most recently settled soccer market for a channel, or None."""
        return self._market_one(
            "m.guild_id = ? AND m.channel_id = ? AND m.status = 'settled' "
            "AND m.kind = 'soccer'",
            (str(guild_id), str(channel_id)),
            ' ORDER BY m.settled_at DESC, m.market_id DESC LIMIT 1')

    # -- Lifecycle writes (shared base) --

    def _set_market_field(self, market_id, column, value):
        """Write one Discord-id column on a market. Column is whitelisted —
        never pass user input as the column name."""
        assert column in ('message_id', 'thread_id', 'thread_intro_id'), column
        self.conn.execute(
            f'UPDATE bet_market SET {column} = ? WHERE market_id = ?',
            (str(value), market_id))
        self.conn.commit()

    def bet_market_set_thread(self, market_id, thread_id):
        self._set_market_field(market_id, 'thread_id', thread_id)

    def bet_market_set_message(self, market_id, message_id):
        self._set_market_field(market_id, 'message_id', message_id)

    def bet_market_set_thread_intro(self, market_id, message_id):
        self._set_market_field(market_id, 'thread_intro_id', message_id)

    def bet_market_mark_thread_locked(self, market_id):
        self.conn.execute(
            'UPDATE bet_market SET thread_locked = 1 WHERE market_id = ?',
            (market_id,))
        self.conn.commit()

    def bet_market_close_betting(self, market_id):
        """Lock betting on an open market early. Returns True if it changed."""
        cur = self.conn.execute(
            "UPDATE bet_market SET bets_closed = 1 "
            "WHERE market_id = ? AND status = 'open' AND bets_closed = 0",
            (market_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # -- Soccer market creation / dedup / odds --

    def bet_market_create(self, guild_id, channel_id, event_id, sport_key,
                          home_team, away_team, commence_time,
                          odds_home, odds_draw, odds_away, created_by, created_at):
        """Open a soccer market and return its id, or None if already open."""
        from tle.util.db.user_db_conn import bet_fixture_key
        guild_id, channel_id, event_id = str(guild_id), str(channel_id), str(event_id)
        fixture_key = bet_fixture_key(
            sport_key, home_team, away_team, commence_time)
        try:
            with self.conn:
                cur = self.conn.execute(
                    'INSERT INTO bet_market '
                    '(kind, guild_id, channel_id, event_id, fixture_key, '
                    'created_by, created_at, closes_at, status) '
                    "SELECT 'soccer', ?, ?, ?, ?, ?, ?, ?, 'open' "
                    'WHERE NOT EXISTS ('
                    "    SELECT 1 FROM bet_market "
                    "    WHERE guild_id = ? AND event_id = ? AND status = 'open'"
                    ') AND NOT EXISTS ('
                    "    SELECT 1 FROM bet_market "
                    "    WHERE guild_id = ? AND fixture_key = ? AND status = 'open'"
                    ')',
                    (guild_id, channel_id, event_id, fixture_key, str(created_by),
                     created_at, commence_time, guild_id, event_id, guild_id,
                     fixture_key)
                )
                if not cur.rowcount:
                    return None
                market_id = cur.lastrowid
                self.conn.execute(
                    'INSERT INTO bet_market_soccer '
                    '(market_id, sport_key, home_team, away_team, odds_home, '
                    'odds_draw, odds_away) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (market_id, sport_key, home_team, away_team,
                     odds_home, odds_draw, odds_away)
                )
        except sqlite3.IntegrityError as e:
            # A concurrent opener won the partial-unique dedup race.
            if 'UNIQUE constraint failed' in str(e):
                return None
            raise
        return market_id

    def bet_market_exists_open_for_event(self, guild_id, event_id):
        """True if the guild already has an open market on this event."""
        row = self.conn.execute(
            "SELECT 1 FROM bet_market WHERE guild_id = ? AND event_id = ? "
            "AND status = 'open' AND kind = 'soccer' LIMIT 1",
            (str(guild_id), str(event_id))).fetchone()
        return row is not None

    def bet_market_has_earlier_open_at_kickoff(self, guild_id, channel_id,
                                               commence_time, market_id):
        """True if another open soccer market in the same channel shares this
        kickoff time and was created earlier (smaller market_id).

        Used to ping the notify role only once when several games kick off at
        the same time in one channel: the earliest-created market of the group
        pings, the rest stay quiet.
        """
        row = self.conn.execute(
            "SELECT 1 FROM bet_market "
            "WHERE guild_id = ? AND channel_id = ? AND status = 'open' "
            "AND kind = 'soccer' AND closes_at = ? AND market_id < ? LIMIT 1",
            (str(guild_id), str(channel_id), commence_time, market_id)
        ).fetchone()
        return row is not None

    def bet_market_count_wagers(self, market_id):
        """Number of wagers placed on a market."""
        return self.conn.execute(
            'SELECT COUNT(*) AS cnt FROM bet_wager WHERE market_id = ?',
            (market_id,)).fetchone().cnt

    def bet_market_set_odds(self, market_id, odds_home, odds_draw, odds_away):
        """Override an open soccer market's frozen odds (only safe with no
        wagers yet — the caller enforces that). Returns True if a row changed."""
        cur = self.conn.execute(
            'UPDATE bet_market_soccer SET odds_home = ?, odds_draw = ?, '
            'odds_away = ? WHERE market_id = ? AND EXISTS ('
            '    SELECT 1 FROM bet_market m '
            '    WHERE m.market_id = bet_market_soccer.market_id '
            "    AND m.status = 'open')",
            (odds_home, odds_draw, odds_away, market_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    # -- Wager reads (shared ``bet_wager``) --

    def bet_get_wager(self, market_id, user_id, pick=None):
        """A user's wager on a market, optionally scoped to one pick."""
        if pick is not None:
            return self.conn.execute(
                'SELECT market_id, user_id, pick, stake, placed_at '
                'FROM bet_wager WHERE market_id = ? AND user_id = ? AND pick = ?',
                (market_id, str(user_id), str(pick))).fetchone()
        return self.conn.execute(
            'SELECT market_id, user_id, pick, stake, placed_at '
            'FROM bet_wager WHERE market_id = ? AND user_id = ? '
            'ORDER BY placed_at DESC, pick ASC LIMIT 1',
            (market_id, str(user_id))).fetchone()

    def bet_get_wagers_for_user(self, market_id, user_id):
        """All of a user's wagers on one market, earliest first."""
        return self.conn.execute(
            'SELECT market_id, user_id, pick, stake, placed_at '
            'FROM bet_wager WHERE market_id = ? AND user_id = ? '
            'ORDER BY placed_at ASC, pick ASC',
            (market_id, str(user_id))).fetchall()

    def bet_get_wagers(self, market_id):
        """All wagers on a market, earliest first."""
        return self.conn.execute(
            'SELECT market_id, user_id, pick, stake, placed_at '
            'FROM bet_wager WHERE market_id = ? ORDER BY placed_at ASC',
            (market_id,)).fetchall()

    def bet_market_pool(self, market_id):
        """(pick, count, total_stake) grouped by pick."""
        return self.conn.execute(
            'SELECT pick, COUNT(*) AS cnt, SUM(stake) AS total '
            'FROM bet_wager WHERE market_id = ? GROUP BY pick',
            (market_id,)).fetchall()

    def bet_market_pool_totals(self, market_id):
        """(total_pool, {pick: staked}) for a market."""
        totals = {p.pick: (p.total or 0) for p in self.bet_market_pool(market_id)}
        return sum(totals.values()), totals

    # -- Pick math / kind policies (dispatched through ``_backend``) --

    def bet_market_title(self, market):
        return _backend(market).title(self, market)

    def bet_market_bettable(self, market):
        """True while the market can still take a wager."""
        return (market.status == 'open' and not market.bets_closed
                and time.time() < market.closes_at)

    def bet_market_allows_remove(self, market):
        """True if a bettor may remove a wager on this market — the inline
        ``0`` shorthand and ``;bet withdraw``. Per-market ``removable`` flag;
        custom (parimutuel) markets open with it off, so their stakes are final
        until cancel/settle, while soccer's fixed-odds game keeps it on."""
        return bool(market.removable)

    def bet_market_allows_draw(self, market):
        return _backend(market).allows_draw(self, market)

    def bet_market_has_fixed_odds(self, market):
        """True only for fixed-odds (soccer) markets — the ones `;bet setodds`
        and `;bet not` can act on."""
        return _backend(market).supports_fixed_odds

    def bet_market_owned_by(self, market, user_id):
        """True when user_id created this market — always False for soccer."""
        return _backend(market).owned_by(market, user_id)

    def bet_pick_odds(self, market, pick):
        return _backend(market).pick_odds(self, market, pick)

    def bet_pick_label(self, market, pick):
        return _backend(market).pick_label(self, market, pick)

    def bet_pick_allowed(self, market, pick):
        return _backend(market).pick_allowed(self, market, pick)

    def bet_market_picks(self, market):
        """Ordered pick keys for the book view."""
        return _backend(market).picks(self, market)

    def bet_pick_potential(self, market, pick, stake):
        """Estimated gross return for a stake at the current state."""
        return _backend(market).pick_potential(self, market, pick, stake)

    # -- Shared stats (consumed by the wallet leaderboards) --

    def bet_has_wagered(self, guild_id, user_id):
        """True if the user has at least one wager on any market in this guild.

        This is the same participation test the balance leaderboard uses, so a
        user counts as an active bettor here exactly when they'd appear there.
        Gitgud coin credits only touch ``bet_wallet`` (never the wager tables),
        so a user who has only ever earned coins this way reads as False.
        """
        row = self.conn.execute(
            'SELECT 1 FROM bet_wager w '
            'JOIN bet_market m ON m.market_id = w.market_id '
            'WHERE m.guild_id = ? AND w.user_id = ? LIMIT 1',
            (str(guild_id), str(user_id))).fetchone()
        return row is not None

    def bet_active_wagers_for_user(self, guild_id, user_id, limit=10):
        """This user's wagers on open markets across both kinds, soonest-closing
        first. Rows carry ``kind``/``closes_at`` plus the wager fields so callers
        render both kinds identically."""
        limit = max(1, min(int(limit), 25))
        return self.conn.execute(
            'SELECT m.*, s.sport_key, s.home_team, s.away_team, s.odds_home, '
            's.odds_draw, s.odds_away, s.result_home, s.result_away, c.title, '
            'w.pick, w.stake, w.placed_at '
            'FROM bet_wager w JOIN bet_market m ON m.market_id = w.market_id '
            'LEFT JOIN bet_market_soccer s ON s.market_id = m.market_id '
            'LEFT JOIN bet_market_custom c ON c.market_id = m.market_id '
            "WHERE m.guild_id = ? AND w.user_id = ? AND m.status = 'open' "
            'ORDER BY m.closes_at ASC, m.market_id ASC LIMIT ?',
            (str(guild_id), str(user_id), limit)).fetchall()
