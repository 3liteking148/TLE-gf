""";atcoder — AtCoder handle linking via affiliation verification.

AtCoder has no public verification API, so account ownership is proven by
asking the user to set their public profile affiliation to a random token;
the bot polls the public profile page until the token appears. Commands are
provided as a mixin inherited by the ``Handles`` cog so errors route through
its ``HandleCogError`` handling.
"""
import asyncio
import random
import string

import discord
from discord.ext import commands

from tle import constants
from tle.util import atcoder_api
from tle.util import codeforces_common as cf_common
from tle.util import db
from tle.util import discord_common

from tle.cogs._handles_helpers import HandleCogError

_POLL_INTERVAL = 15
_POLL_ATTEMPTS = 4
_TOKEN_LENGTH = 8
_IDENTIFY_GROUP = 'atcoder_identify'


class AtcoderHandlesMixin:
    """``;atcoder`` group: link and look up AtCoder handles."""

    @commands.group(brief='Link or look up AtCoder handles',
                    invoke_without_command=True)
    async def atcoder(self, ctx):
        """AtCoder handles are verified by setting your profile affiliation
        to a random token. Run `;atcoder identify <handle>` to start."""
        await ctx.send_help(ctx.command)

    @atcoder.command(name='identify', brief='Identify yourself', usage='<handle>')
    @cf_common.user_guard(
        group=_IDENTIFY_GROUP,
        get_exception=lambda: HandleCogError(
            'AtCoder identification is already running for you'))
    async def atcoder_identify(self, ctx, handle: str):
        """Link an AtCoder account to your Discord account by setting your
        AtCoder profile affiliation to a random token within 60 seconds."""
        invoker = str(ctx.author)
        user_db = cf_common.user_db

        user = await atcoder_api.get_user(handle)
        if user is None:
            raise HandleCogError(f'AtCoder user `{handle}` not found')
        handle = user.handle

        existing = user_db.get_atcoder_handle(ctx.author.id, ctx.guild.id)
        if existing:
            raise HandleCogError(f'{ctx.author.mention}, your AtCoder handle is '
                                 f'already set to `{existing}`. Ask an Admin or '
                                 'Moderator to change it.')

        claimed_by = user_db.get_atcoder_user_id(handle, ctx.guild.id)
        if claimed_by and claimed_by != ctx.author.id:
            raise HandleCogError(f'The handle `{handle}` is already associated '
                                 'with another user. Ask an Admin or Moderator '
                                 'in case of an inconsistency.')

        token = 'tle-' + ''.join(random.choices(
            string.ascii_lowercase + string.digits, k=_TOKEN_LENGTH))
        await ctx.send(f'`{invoker}`, set your AtCoder profile affiliation to '
                       f'**`{token}`** '
                       f'(<https://atcoder.jp/settings>) within 60 seconds')
        for _ in range(_POLL_ATTEMPTS):
            await asyncio.sleep(_POLL_INTERVAL)
            user = await atcoder_api.get_user(handle)
            if user is not None and user.affiliation == token:
                try:
                    user_db.set_atcoder_handle(ctx.author.id, ctx.guild.id, handle)
                except db.UniqueConstraintFailed:
                    raise HandleCogError(f'The handle `{handle}` is already '
                                         'associated with another user.')
                embed = discord_common.embed_success(
                    f'AtCoder handle for {ctx.author.mention} successfully '
                    f'set to **{handle}**')
                await ctx.send(embed=embed)
                await ctx.send('You can now revert your affiliation.')
                return
        await ctx.send(f'Sorry `{invoker}`, can you try again? Remember: set '
                       'your affiliation to the token exactly, then wait for '
                       'confirmation.')

    @atcoder.command(name='get', brief='Show AtCoder handle of a user',
                     usage='[member]')
    async def atcoder_get(self, ctx, member: discord.Member = None):
        """Show the AtCoder handle (and live profile info) of a user."""
        member = member or ctx.author
        handle = cf_common.user_db.get_atcoder_handle(member.id, ctx.guild.id)
        if not handle:
            raise HandleCogError(f'AtCoder handle for {member.mention} not '
                                 'found in database')
        desc = (f'AtCoder handle for {member.mention} is '
                f'**[{handle}](https://atcoder.jp/users/{handle})**')
        embed = discord.Embed(description=desc)
        user = await atcoder_api.get_user(handle)
        if user is not None:
            embed.add_field(name='Rating', value=user.rating or 'Unrated',
                            inline=True)
            embed.add_field(name='Country', value=user.country or 'Unknown',
                            inline=True)
        await ctx.send(embed=embed)

    @atcoder.command(name='remove', brief='Unlink AtCoder handle',
                     usage='<handle>')
    @commands.has_any_role(*constants.TLE_ADMIN, *constants.TLE_MODERATOR)
    async def atcoder_remove(self, ctx, handle: str):
        """Remove an AtCoder handle from the database."""
        user_id = cf_common.user_db.get_atcoder_user_id(handle, ctx.guild.id)
        if user_id is None:
            raise HandleCogError(f'`{handle}` not found in database')
        cf_common.user_db.remove_atcoder_handle(handle, ctx.guild.id)
        embed = discord_common.embed_success(
            f'Removed `{handle}` from database')
        await ctx.send(embed=embed)
