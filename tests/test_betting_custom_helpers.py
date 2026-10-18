"""Pure parsing tests for user-created markets (`;bet open` grammar, choice
slugs and pick resolution)."""
from collections import namedtuple

import pytest

from tle.cogs._betting_custom import (
    build_choices, choice_key, parse_duration, parse_open_args,
    resolve_custom_pick,
)

Choice = namedtuple('Choice', 'choice_key label')


@pytest.mark.parametrize('token,seconds', [
    ('2h', 7200), ('45m', 2700), ('1d', 86400), (' 3H ', 10800)])
def test_parse_duration_units(token, seconds):
    assert parse_duration(token) == seconds


@pytest.mark.parametrize('token', ['0h', '2x', 'h', None])
def test_parse_duration_invalid(token):
    assert parse_duration(token) is None


@pytest.mark.parametrize('label,key', [
    ('Yes', 'yes'), ('Côte d’Ivoire', 'cotedivoire'), ('Red Team!', 'redteam'),
    ('', 'choice'), ('!!!', 'choice')])
def test_choice_key(label, key):
    assert choice_key(label) == key


def test_build_choices_valid():
    assert build_choices(['Yes', 'No']) == [('yes', 'Yes'), ('no', 'No')]
    assert build_choices(['  Red   Team ']) == [('redteam', 'Red Team')]


@pytest.mark.parametrize('labels,message', [
    (['Yes', 'YES!'], 'Duplicate'),
    (['Yes', '   '], 'needs a name'),
    (['one two three four'], 'words'),
    (['x' * 41], 'too long'),
])
def test_build_choices_rejected(labels, message):
    with pytest.raises(ValueError, match=message):
        build_choices(labels)


def test_parse_open_args():
    assert parse_open_args('Will it rain? :: Yes | No') == \
        ('Will it rain?', ['Yes', 'No'], None)
    assert parse_open_args('Rain? :: Yes | No :: 2h') == \
        ('Rain?', ['Yes', 'No'], 7200)
    assert parse_open_args('A :: x | y | z') == ('A', ['x', 'y', 'z'], None)


@pytest.mark.parametrize('text', [
    'Will it rain? Yes No',
    'Title :: OnlyOne',
    'Title :: ' + ' | '.join(f'c{i}' for i in range(11)),
    ' :: Yes | No',
    'Title :: Yes | No :: soon',
])
def test_parse_open_args_rejected(text):
    assert parse_open_args(text) is None


CHOICES = [Choice('yes', 'Yes'), Choice('no', 'No'),
           Choice('maybe', 'Maybe Later')]


def test_resolve_exact_label_and_key():
    assert resolve_custom_pick('Yes', CHOICES) == 'yes'
    assert resolve_custom_pick('yes', CHOICES) == 'yes'
    assert resolve_custom_pick('MAYBE LATER', CHOICES) == 'maybe'


def test_resolve_unambiguous_prefix():
    assert resolve_custom_pick('may', CHOICES) == 'maybe'


def test_resolve_ambiguous_prefix_is_none():
    choices = [Choice('red', 'Red'), Choice('really', 'Really')]
    assert resolve_custom_pick('re', choices) is None


def test_resolve_unknown_is_none():
    assert resolve_custom_pick('banana', CHOICES) is None
    assert resolve_custom_pick(None, CHOICES) is None
