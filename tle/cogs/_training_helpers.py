"""Pure helpers, enums, and the Game state machine for the training cog.

Split out of ``training.py`` to keep each module under the line limit. The
``Training`` cog imports everything it needs from here.
"""
from enum import IntEnum

from discord.ext import commands

from tle.util import codeforces_common as cf_common
# TrainingProblemStatus is a DB enum re-exported here because _training_impl
# imports it from this module (the pre-split training.py imported it directly
# from user_db_conn).
from tle.util.db.user_db_conn import TrainingProblemStatus  # noqa: F401
# Ranking-table image rendering + colour/fonts now live in a shared util;
# re-exported so existing tle.cogs.training.rating_to_color / FONTS references work.
from tle.util.ranking_image import (  # noqa: F401
    FONTS, rating_to_color, render_ranking_table_image)


_TRAINING_MIN_RATING_VALUE = cf_common.RATING_MIN
_TRAINING_MAX_RATING_VALUE = cf_common.RATING_MAX


class TrainingMode(IntEnum):
    NORMAL = 0
    SURVIVAL = 1
    TIMED15 = 2
    TIMED30 = 3
    TIMED60 = 4
    TIMED1 = 5


class TrainingResult(IntEnum):
    SOLVED = 0
    TOOSLOW = 1
    SKIPPED = 2
    INVALIDATED = 3


class TrainingCogError(commands.CommandError):
    pass


def get_fastest_solves_image(rankings):
    """return PIL image for rankings"""
    rows = [(str(pos), name, handle, rating,
             cf_common.pretty_time_format(time, shorten=True, always_seconds=True))
            for pos, name, handle, rating, time in rankings]
    return render_ranking_table_image(
        rows, headers=('Rating', 'Name', 'Handle', 'Time'),
        filename='fastesttraining.png', width=1000, rank_ratio=0.10, name_ratio=0.35)




class Game:
    def __init__(self, mode, score=None, lives=None, timeleft=None):
        self.mode = int(mode)
        # existing game
        if score is not None:
            self.score = int(score)
            self.lives = int(lives) if lives is not None else lives
            self.timeleft = int(timeleft) if timeleft is not None else timeleft
            self.alive = True if self.lives is None or self.lives > 0 else False
            return
        # else we init a new game
        self.timeleft = self._getBaseTime()
        self.lives = self._getBaseLives()
        self.alive = True
        self.score = int(0)

    def _getModeStr(self):
        if self.mode == TrainingMode.NORMAL:
            return "Infinite"
        elif self.mode == TrainingMode.SURVIVAL:
            return "Survival"
        elif self.mode == TrainingMode.TIMED1:
            return "Timed 1 mins"
        elif self.mode == TrainingMode.TIMED15:
            return "Timed 15 mins"
        elif self.mode == TrainingMode.TIMED30:
            return "Timed 30 mins"
        elif self.mode == TrainingMode.TIMED60:
            return "Timed 60 mins"

    def _getBaseLives(self):
        if self.mode == TrainingMode.NORMAL:
            return None
        else:
            return 3

    def _getBaseTime(self):
        if self.mode == TrainingMode.NORMAL or self.mode == TrainingMode.SURVIVAL:
            return None
        if self.mode == TrainingMode.TIMED1:
            return int(1*60+1)
        if self.mode == TrainingMode.TIMED15:
            return int(15*60+1)
        if self.mode == TrainingMode.TIMED30:
            return int(30*60+1)
        if self.mode == TrainingMode.TIMED60:
            return int(60*60+1)

    def _newRating(self, success, rating):
        newRating = rating
        if success == TrainingResult.SOLVED:
            newRating += 100
        else:
            newRating -= 100
        newRating = min(newRating, _TRAINING_MAX_RATING_VALUE)
        newRating = max(newRating, _TRAINING_MIN_RATING_VALUE)
        return newRating

    def doSolved(self, rating, duration):
        rating = int(rating)
        success = TrainingResult.SOLVED
        if self.mode != TrainingMode.NORMAL and self.mode != TrainingMode.SURVIVAL:
            if duration > self.timeleft:
                success = TrainingResult.TOOSLOW
                self.lives -= 1
                self.timeleft = self._getBaseTime()
                if self.lives is not None and self.lives == 0:
                    self.alive = False
            else:
                self.score += 1
                self.timeleft = int(
                    min(self.timeleft - duration + self._getBaseTime(), 2*self._getBaseTime()))
        else:
            self.score += 1
        newRating = self._newRating(success, rating)
        return success, newRating

    def doSkip(self, rating, duration):
        rating = int(rating)
        success = TrainingResult.SKIPPED
        if self.mode != TrainingMode.NORMAL:
            self.lives -= 1
            if self.lives is not None and self.lives == 0:
                self.alive = False

        self.timeleft = self._getBaseTime()
        newRating = self._newRating(success, rating)
        return success, newRating

    def doFinish(self, rating, duration):
        success = TrainingResult.INVALIDATED
        self.alive = False
        return success, rating
