"""Standard competition ranking ("1224") for leaderboards.

Leaderboards across the bot used to number rows by their position in the
sorted list (``enumerate`` index + 1). That gives tied entries different
ranks based on whatever secondary sort the query happened to use — e.g.
three users tied for the top score would show as #1, #2, #3 ordered by
``user_id``. The correct behaviour is *standard competition ranking*: tied
entries share the lowest rank in their group, and the next distinct score
skips ahead by the size of the group::

    scores: 10 10 10  7  5  5  3
    ranks:   1  1  1  4  5  5  7

This module centralises that logic so every leaderboard stays consistent.
"""

_UNSET = object()


def competition_ranks(keys):
    """Return the standard-competition rank for each key.

    ``keys`` must already be ordered the way they will be displayed, with
    equal keys adjacent (the usual ``ORDER BY score DESC`` output). Returns a
    list of 1-based ranks parallel to ``keys``: equal keys get the same rank,
    and the rank after a tie group jumps by the group's size.
    """
    ranks = []
    prev = _UNSET
    current = 0
    for i, key in enumerate(keys):
        if prev is _UNSET or key != prev:
            current = i + 1
        ranks.append(current)
        prev = key
    return ranks


def rank_items(items, key):
    """Yield ``(rank, item)`` pairs using standard competition ranking.

    ``items`` must already be sorted into display order (equal keys adjacent).
    ``key`` extracts the comparable score from each item. This is the
    convenience wrapper most call sites want — compute ranks over the *whole*
    ordered list once, then paginate/format the resulting pairs so ties that
    straddle a page boundary still share a rank.
    """
    ranks = competition_ranks([key(item) for item in items])
    return list(zip(ranks, items))
