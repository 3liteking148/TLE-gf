"""Tests for the shared standard-competition-ranking helper.

These pin the '1224' semantics: tied scores share the lowest rank in their
group and the rank after a tie skips ahead by the group's size.
"""
from collections import namedtuple

from tle.util import ranking


class TestCompetitionRanks:
    def test_all_distinct(self):
        assert ranking.competition_ranks([10, 7, 5, 3]) == [1, 2, 3, 4]

    def test_three_way_tie_for_first(self):
        # The headline example: three tied for first are all rank 1, the next
        # distinct score is rank 4 (not 2).
        assert ranking.competition_ranks([10, 10, 10, 7]) == [1, 1, 1, 4]

    def test_tie_in_the_middle(self):
        assert ranking.competition_ranks([10, 7, 7, 5]) == [1, 2, 2, 4]

    def test_tie_at_the_bottom(self):
        assert ranking.competition_ranks([10, 7, 5, 5]) == [1, 2, 3, 3]

    def test_multiple_separate_ties(self):
        assert ranking.competition_ranks([9, 9, 7, 5, 5, 5, 2]) == \
            [1, 1, 3, 4, 4, 4, 7]

    def test_everyone_tied(self):
        assert ranking.competition_ranks([4, 4, 4, 4]) == [1, 1, 1, 1]

    def test_single_element(self):
        assert ranking.competition_ranks([42]) == [1]

    def test_empty(self):
        assert ranking.competition_ranks([]) == []

    def test_zero_is_a_real_key_not_treated_as_unset(self):
        # The internal "previous key" sentinel must not collide with a real
        # 0 score — two leading zeros should still tie.
        assert ranking.competition_ranks([0, 0, -1]) == [1, 1, 3]

    def test_does_not_reorder(self):
        # The helper assumes the caller already sorted; it must not sort.
        # Equal keys that are not adjacent get distinct groups by design.
        assert ranking.competition_ranks([10, 5, 10]) == [1, 2, 3]


class TestRankItems:
    def _row(self, uid, score):
        Row = namedtuple('Row', 'uid score')
        return Row(uid, score)

    def test_pairs_rank_with_item(self):
        rows = [self._row('a', 10), self._row('b', 10), self._row('c', 4)]
        ranked = ranking.rank_items(rows, lambda r: r.score)
        assert [rank for rank, _ in ranked] == [1, 1, 3]
        assert [row.uid for _, row in ranked] == ['a', 'b', 'c']

    def test_key_can_index_tuples(self):
        rows = [('x', 5), ('y', 5), ('z', 1)]
        ranked = ranking.rank_items(rows, lambda item: item[1])
        assert ranked == [(1, ('x', 5)), (1, ('y', 5)), (3, ('z', 1))]

    def test_empty(self):
        assert ranking.rank_items([], lambda r: r) == []

    def test_preserves_input_order_within_tie(self):
        rows = [self._row('first', 7), self._row('second', 7)]
        ranked = ranking.rank_items(rows, lambda r: r.score)
        assert [row.uid for _, row in ranked] == ['first', 'second']
