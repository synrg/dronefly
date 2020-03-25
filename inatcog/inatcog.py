"""A cog for using the iNaturalist platform."""
from abc import ABC
from math import ceil
import re
from typing import Union
import asyncio
import discord
import inflect
from redbot.core import checks, commands, Config
from redbot.core.utils.menus import menu, start_adding_reactions, DEFAULT_CONTROLS
from pyparsing import ParseException
from .api import INatAPI, WWW_BASE_URL
from .common import grouper
from .converters import QuotedContextMemberConverter, InheritableBoolConverter
from .embeds import make_embed, sorry
from .last import INatLinkMsg
from .obs import get_obs_fields, maybe_match_obs, PAT_OBS_LINK
from .parsers import RANK_EQUIVALENTS, RANK_KEYWORDS
from .places import INatPlaceTable, RESERVED_PLACES
from .projects import UserProject, ObserverStats
from .listeners import Listeners
from .taxa import FilteredTaxon, INatTaxaQuery, get_taxon
from .users import INatUserTable, PAT_USER_LINK, User

_SCHEMA_VERSION = 2
_DEVELOPER_BOT_IDS = [614037008217800707, 620938327293558794]
_INAT_GUILD_ID = 525711945270296587
SPOILER_PAT = re.compile(r"\|\|")
DOUBLE_BAR_LIT = "\\|\\|"


class CompositeMetaClass(type(commands.Cog), type(ABC)):
    """
    See https://github.com/mikeshardmind/SinbadCogs/blob/v3/rolemanagement/core.py
    """


# pylint: disable=too-many-ancestors
class INatCog(Listeners, commands.Cog, metaclass=CompositeMetaClass):
    """The main iNaturalist cog class."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1607)
        self.api = INatAPI()
        self.p = inflect.engine()  # pylint: disable=invalid-name
        self.taxa_query = INatTaxaQuery(self)
        self.user_table = INatUserTable(self)
        self.place_table = INatPlaceTable(self)
        self.user_cache_init = {}

        self.config.register_global(schema_version=1)
        self.config.register_guild(
            autoobs=False,
            user_projects={},
            places={},
            project_emojis={33276: "<:discord:638537174048047106>", 15232: ":poop:"},
        )
        self.config.register_channel(autoobs=None)
        self.config.register_user(
            home=None, inat_user_id=None, known_in=[], known_all=False
        )
        self._cleaned_up = False
        self._init_task: asyncio.Task = self.bot.loop.create_task(self.initialize())
        self._ready_event: asyncio.Event = asyncio.Event()

    async def cog_before_invoke(self, ctx: commands.Context):
        await self._ready_event.wait()

    async def initialize(self) -> None:
        """Initialization after bot is ready."""
        await self.bot.wait_until_ready()
        await self._migrate_config(await self.config.schema_version(), _SCHEMA_VERSION)
        self._ready_event.set()

    async def _migrate_config(self, from_version: int, to_version: int) -> None:
        if from_version == to_version:
            return

        if from_version < 2 <= to_version:
            # Initial registrations via the developer's own bot were intended
            # to be for the iNat server only. Prevent leakage to other servers.
            # Any other servers using this feature with schema 1 must now
            # re-register each user, or the user must `[p]inat user set known
            # true` to be known in other servers.
            if self.bot.user.id in _DEVELOPER_BOT_IDS:
                all_users = await self.config.all_users()
                for (user_id, user_value) in all_users.items():
                    if user_value["inat_user_id"]:
                        await self.config.user_from_id(int(user_id)).known_in.set(
                            [_INAT_GUILD_ID]
                        )
            await self.config.schema_version.set(2)

    def cog_unload(self):
        """Cleanup when the cog unloads."""
        if not self._cleaned_up:
            self.api.session.detach()
            if self._init_task:
                self._init_task.cancel()
            self._cleaned_up = True

    @commands.group()
    async def inat(self, ctx):
        """Access the iNat platform.

        Note: When configured as recommended, single word command aliases are
        defined for every `[p]inat` subcommand, `[p]family` is an alias for
        `[p]inat taxon family`, and likewise for all other ranks. See the
        help topics for each subcommand for details.
        """

    @inat.group(invoke_without_command=True)
    @checks.admin_or_permissions(manage_messages=True)
    async def autoobs(self, ctx, state: InheritableBoolConverter):
        """Set auto-observation mode for channel (mods only).

        To set the mode for the channel:
        ```
        [p]inat autoobs on
        [p]inat autoobs off
        [p]inat autoobs inherit
        ```
        When `inherit` is specified, channel mode inherits from the server
        setting.
        """
        if ctx.author.bot or ctx.guild is None:
            return

        config = self.config.channel(ctx.channel)
        await config.autoobs.set(state)

        if state is None:
            server_state = await self.config.guild(ctx.guild).autoobs()
            value = f"inherited from server ({'on' if server_state else 'off'})"
        else:
            value = "on" if state else "off"
        await ctx.send(f"Channel observation auto-preview is {value}.")
        return

    @autoobs.command()
    @checks.admin_or_permissions(manage_messages=True)
    async def server(self, ctx, state: bool):
        """Set auto-observation mode for server (mods only).

        ```
        [p]inat autoobs server on
        [p]inat autoobs server off
        ```
        """
        if ctx.author.bot or ctx.guild is None:
            return

        config = self.config.guild(ctx.guild)
        await config.autoobs.set(state)
        await ctx.send(
            f"Server observation auto-preview is {'on' if state else 'off'}."
        )
        return

    @autoobs.command()
    async def show(self, ctx):
        """Show auto-observation mode for channel & server.

        ```
        [p]inat autoobs show
        ```
        """
        if ctx.author.bot or ctx.guild is None:
            return

        server_config = self.config.guild(ctx.guild)
        server_state = await server_config.autoobs()
        await ctx.send(
            f"Server observation auto-preview is {'on' if server_state else 'off'}."
        )
        channel_config = self.config.channel(ctx.channel)
        channel_state = await channel_config.autoobs()
        if channel_state is None:
            value = f"inherited from server ({'on' if server_state else 'off'})"
        else:
            value = "on" if channel_state else "off"
        await ctx.send(f"Channel observation auto-preview is {value}.")
        return

    @inat.group()
    async def last(self, ctx):
        """Show info for recently mentioned iNat page."""

    async def get_last_obs_from_history(self, ctx):
        """Get last obs from history."""
        msgs = await ctx.history(limit=1000).flatten()
        inat_link_msg = INatLinkMsg(self.api)
        return await inat_link_msg.get_last_obs_msg(msgs)

    async def get_last_taxon_from_history(self, ctx):
        """Get last taxon from history."""
        msgs = await ctx.history(limit=1000).flatten()
        inat_link_msg = INatLinkMsg(self.api)
        return await inat_link_msg.get_last_taxon_msg(msgs)

    @last.group(name="obs", aliases=["observation"], invoke_without_command=True)
    async def last_obs(self, ctx):
        """Show recently mentioned iNat observation."""
        last = await self.get_last_obs_from_history(ctx)
        if not (last and last.obs):
            await ctx.send(embed=sorry(apology="Nothing found"))
            return

        await ctx.send(embed=await self.make_last_obs_embed(ctx, last))
        if last.obs.sound:
            await self.maybe_send_sound_url(ctx.channel, last.obs.sound)

    @last_obs.command(name="img", aliases=["image"])
    async def last_obs_img(self, ctx, number=None):
        """Show image for recently mentioned iNat observation."""
        last = await self.get_last_obs_from_history(ctx)
        if last.obs and last.obs.taxon:
            try:
                num = 1 if number is None else int(number)
            except ValueError:
                num = 0
            await ctx.send(
                embed=await self.make_obs_embed(
                    ctx.guild, last.obs, last.url, preview=num
                )
            )
        else:
            await ctx.send(embed=sorry(apology="Nothing found"))

    @last_obs.command(name="taxon", aliases=["t"])
    async def last_obs_taxon(self, ctx):
        """Show taxon for recently mentioned iNat observation."""
        last = await self.get_last_obs_from_history(ctx)
        if last and last.obs and last.obs.taxon:
            await self.send_embed_for_taxon(ctx, last.obs.taxon)
        else:
            await ctx.send(embed=sorry(apology="Nothing found"))

    @last_obs.command(name="map", aliases=["m"])
    async def last_obs_map(self, ctx):
        """Show map for recently mentioned iNat observation."""
        last = await self.get_last_obs_from_history(ctx)
        if last and last.obs and last.obs.taxon:
            await ctx.send(embed=await self.make_map_embed([last.obs.taxon]))
        else:
            await ctx.send(embed=sorry(apology="Nothing found"))

    @last_obs.command(name="<rank>", aliases=RANK_KEYWORDS)
    async def last_obs_rank(self, ctx):
        """Show the `<rank>` of the last observation (e.g. `family`).

        `[p]inat last obs family`      show family of last obs
        `[p]inat last obs superfamily` show superfamily of last obs

        Any rank known to iNat can be specified.
        """
        last = await self.get_last_obs_from_history(ctx)
        if not (last and last.obs):
            await ctx.send(embed=sorry(apology="Nothing found"))
            return

        rank = ctx.invoked_with
        if rank == "<rank>":
            await ctx.send_help()
            return

        rank_keyword = RANK_EQUIVALENTS.get(rank) or rank
        if last.obs.taxon.rank == rank_keyword:
            await self.send_embed_for_taxon(ctx, last.obs.taxon)
        elif last.obs.taxon:
            full_record = await get_taxon(self, last.obs.taxon.taxon_id)
            ancestor = await self.taxa_query.get_taxon_ancestor(
                full_record, rank_keyword
            )
            if ancestor:
                await self.send_embed_for_taxon(ctx, ancestor)
            else:
                await ctx.send(
                    embed=sorry(
                        apology=f"The last observation has no {rank_keyword} ancestor."
                    )
                )
        else:
            await ctx.send(embed=sorry(apology="The last observation has no taxon."))

    @last.group(name="taxon", aliases=["t"], invoke_without_command=True)
    async def last_taxon(self, ctx):
        """Show recently mentioned iNat taxon."""
        last = await self.get_last_taxon_from_history(ctx)
        if not (last and last.taxon):
            await ctx.send(embed=sorry(apology="Nothing found"))
            return

        await self.send_embed_for_taxon(ctx, last.taxon)

    @last_taxon.command(name="by")
    async def last_taxon_by(self, ctx, user: QuotedContextMemberConverter):
        """Show recently mentioned taxon with observation counts for a user."""
        last = await self.get_last_taxon_from_history(ctx)
        if not (last and last.taxon):
            await ctx.send(embed=sorry(apology="Nothing found"))
            return

        inat_user = await self.user_table.get_user(user.member)
        filtered_taxon = FilteredTaxon(last.taxon, inat_user, None, None)
        await self.send_embed_for_taxon(ctx, filtered_taxon)

    @last_taxon.command(name="from")
    async def last_taxon_from(self, ctx, place: str):
        """Show recently mentioned taxon with observation counts for a place."""
        last = await self.get_last_taxon_from_history(ctx)
        if not (last and last.taxon):
            await ctx.send(embed=sorry(apology="Nothing found"))
            return

        try:
            place = await self.place_table.get_place(ctx.guild, place, ctx.author)
        except LookupError:
            place = None
        filtered_taxon = FilteredTaxon(last.taxon, None, place, None)
        await self.send_embed_for_taxon(ctx, filtered_taxon)

    @last_taxon.command(name="map", aliases=["m"])
    async def last_taxon_map(self, ctx):
        """Show map for recently mentioned taxon."""
        last = await self.get_last_taxon_from_history(ctx)
        if not (last and last.taxon):
            await ctx.send(embed=sorry(apology="Nothing found"))
            return

        await ctx.send(embed=await self.make_map_embed([last.taxon]))

    @last_taxon.command(name="image", aliases=["img"])
    async def last_taxon_image(self, ctx):
        """Show image for recently mentioned taxon."""
        last = await self.get_last_taxon_from_history(ctx)
        if not (last and last.taxon):
            await ctx.send(embed=sorry(apology="Nothing found"))
            return

        await self.send_embed_for_taxon_image(ctx, last.taxon)

    @last_taxon.command(name="<rank>", aliases=RANK_KEYWORDS)
    async def last_taxon_rank(self, ctx):
        """Show the `<rank>` of the last taxon (e.g. `family`).

        `[p]inat last taxon family`      show family of last taxon
        `[p]inat last taxon superfamily` show superfamily of last taxon

        Any rank known to iNat can be specified.
        """
        rank = ctx.invoked_with
        if rank == "<rank>":
            await ctx.send_help()
            return

        last = await self.get_last_taxon_from_history(ctx)
        if not (last and last.taxon):
            await ctx.send(embed=sorry(apology="Nothing found"))
            return

        rank_keyword = RANK_EQUIVALENTS.get(rank) or rank
        if last.taxon.rank == rank_keyword:
            await self.send_embed_for_taxon(ctx, last.taxon)
        else:
            full_record = await get_taxon(self, last.taxon.taxon_id)
            ancestor = await self.taxa_query.get_taxon_ancestor(
                full_record, rank_keyword
            )
            if ancestor:
                await self.send_embed_for_taxon(ctx, ancestor)
            else:
                await ctx.send(
                    embed=sorry(apology=f"The last taxon has no {rank} ancestor.")
                )

    @inat.command()
    async def link(self, ctx, *, query):
        """Show summary for iNaturalist link.

        e.g.
        ```
        [p]inat link https://inaturalist.org/observations/#
           -> an embed summarizing the observation link
        ```
        When configured as recommended,
        `[p]link` is an alias for `[p]inat link`.
        """
        mat = re.search(PAT_OBS_LINK, query)
        if mat:
            obs_id = int(mat["obs_id"] or mat["cmd_obs_id"])
            url = mat["url"]

            results = (await self.api.get_observations(obs_id, include_new_projects=1))[
                "results"
            ]
            obs = get_obs_fields(results[0]) if results else None
            await ctx.send(embed=await self.make_obs_embed(ctx.guild, obs, url))
            if obs and obs.sound:
                await self.maybe_send_sound_url(ctx.channel, obs.sound)
        else:
            await ctx.send(embed=sorry())

    @inat.command()
    async def map(self, ctx, *, taxa_list):
        """Show range map for a list of one or more taxa.

        **Examples:**
        ```
        [p]inat map polar bear
        [p]inat map 24255,24267
        [p]inat map boreal chorus frog,western chorus frog
        ```
        See `[p]help inat taxon` for help specifying taxa.

        When configured as recommended,
        `[p]map` is an alias for `[p]inat map`.
        """

        if not taxa_list:
            await ctx.send_help()
            return

        try:
            taxa = await self.taxa_query.query_taxa(taxa_list)
        except ParseException:
            await ctx.send(embed=sorry())
            return
        except LookupError as err:
            reason = err.args[0]
            await ctx.send(embed=sorry(apology=reason))
            return

        await ctx.send(embed=await self.make_map_embed(taxa))

    @inat.group(invoke_without_command=True)
    async def place(self, ctx, *, query):
        """Show a place by number, name, or abbreviation defined with `[p]place add`."""
        try:
            place = await self.place_table.get_place(ctx.guild, query, ctx.author)
            await ctx.send(place.url)
        except LookupError as err:
            await ctx.send(err)

    @place.command(name="add")
    async def place_add(self, ctx, abbrev: str, place_number: int):
        """Add place abbreviation for guild."""
        if not ctx.guild:
            return

        config = self.config.guild(ctx.guild)
        places = await config.places()
        abbrev_lowered = abbrev.lower()
        if abbrev_lowered in RESERVED_PLACES:
            await ctx.send(
                f"Place abbreviation '{abbrev_lowered}' cannot be added as it is reserved."
            )

        if abbrev_lowered in places:
            url = f"{WWW_BASE_URL}/places/{places[abbrev_lowered]}"
            await ctx.send(
                f"Place abbreviation '{abbrev_lowered}' is already defined as: {url}"
            )
            return

        places[abbrev_lowered] = place_number
        await config.places.set(places)
        await ctx.send(f"Place abbreviation added.")

    @place.command(name="remove")
    async def place_remove(self, ctx, abbrev: str):
        """Remove place abbreviation for guild."""
        if not ctx.guild:
            return

        config = self.config.guild(ctx.guild)
        places = await config.places()
        abbrev_lowered = abbrev.lower()

        if abbrev_lowered not in places:
            await ctx.send("Place abbreviation not defined.")
            return

        del places[abbrev_lowered]
        await config.places.set(places)
        await ctx.send("Place abbreviation removed.")

    @inat.command()
    async def obs(self, ctx, *, query):
        """Show observation summary for link or number.

        e.g.
        ```
        [p]inat obs #
           -> an embed summarizing the numbered observation
        [p]inat obs https://inaturalist.org/observations/#
           -> an embed summarizing the observation link (minus the preview,
              which Discord provides itself)
        [p]inat obs insects by kueda
           -> an embed showing counts of insects by user kueda
        [p]inat obs insects from canada
           -> an embed showing counts of insects from Canada
        ```
        When configured as recommended,
        `[p]obs` is an alias for `[p]inat obs`.
        """

        obs, url = await maybe_match_obs(self.api, query, id_permitted=True)
        # Note: if the user specified an invalid or deleted id, a url is still
        # produced (i.e. should 404).
        if url:
            await ctx.send(
                embed=await self.make_obs_embed(ctx.guild, obs, url, preview=False)
            )
            if obs and obs.sound:
                await self.maybe_send_sound_url(ctx.channel, obs.sound)
            return

        try:
            filtered_taxon = await self.taxa_query.query_taxon(ctx, query)
            msg = await ctx.send(embed=await self.make_obs_counts_embed(filtered_taxon))
            group_by = filtered_taxon.group_by
            place = filtered_taxon.place
            user = filtered_taxon.user
            if group_by == "place" or (user and not place):
                start_adding_reactions(msg, ["#️⃣", "📝"])
            # if group_by == "user" or (place and not user):
            #    start_adding_reactions(msg, ["📍", "📝"])
        except ParseException:
            await ctx.send(embed=sorry())
            return
        except LookupError as err:
            reason = err.args[0]
            await ctx.send(embed=sorry(apology=reason))
            return

    @inat.command()
    async def related(self, ctx, *, taxa_list):
        """Relatedness of a list of taxa.

        **Examples:**
        ```
        [p]inat related 24255,24267
        [p]inat related boreal chorus frog,western chorus frog
        ```
        See `[p]help inat taxon` for help specifying taxa.

        When configured as recommended,
        `[p]related` is an alias for `[p]inat related`.
        """

        if not taxa_list:
            await ctx.send_help()
            return

        try:
            taxa = await self.taxa_query.query_taxa(taxa_list)
        except ParseException:
            await ctx.send(embed=sorry())
            return
        except LookupError as err:
            reason = err.args[0]
            await ctx.send(embed=sorry(apology=reason))
            return

        await ctx.send(embed=await self.make_related_embed(taxa))

    @inat.command()
    async def image(self, ctx, *, taxon_query):
        """Show default image for taxon query.

        See `[p]help inat taxon` for `taxon_query` format."""
        try:
            filtered_taxon = await self.taxa_query.query_taxon(ctx, taxon_query)
        except ParseException:
            await ctx.send(embed=sorry())
            return
        except LookupError as err:
            reason = err.args[0]
            await ctx.send(embed=sorry(apology=reason))
            return

        await self.send_embed_for_taxon_image(ctx, filtered_taxon.taxon)

    @inat.command()
    async def taxon(self, ctx, *, query):
        """Show taxon best matching the query.

        - Match the taxon with the given iNat id#.
        - Match words that start with the terms typed.
        - Exactly match words enclosed in double-quotes.
        - Match a taxon 'in' an ancestor taxon.
        - Filter matches by rank keywords before or after other terms.
        - Match the AOU 4-letter code (if it's in iNat's Taxonomy).
        **Examples:**
        ```
        [p]inat taxon bear family
           -> Ursidae (Bears)
        [p]inat taxon prunella
           -> Prunella (self-heals)
        [p]inat taxon prunella in animals
           -> Prunella
        [p]inat taxon wtsp
           -> Zonotrichia albicollis (White-throated Sparrow)
        ```
        When configured as recommended, these aliases save typing:
        - `[p]t` or `[p]taxon` for `[p]inat taxon`
        and all rank keywords also work as command aliases, e.g.
        - `[p]family bear`
        - `[p]t family bear` (both equivalent to the 1st example)
        Abbreviated rank keywords are:
        - `sp` for `species`
        - `ssp` for `subspecies`
        - `gen` for `genus`
        - `var` for `variety`
        Multiple rank keywords may be given, e.g.
        - `[p]t ssp var form domestic duck` to search for domestic
        duck subspecies, variety, or form
        """

        try:
            filtered_taxon = await self.taxa_query.query_taxon(ctx, query)
        except ParseException:
            await ctx.send(embed=sorry())
            return
        except LookupError as err:
            reason = err.args[0]
            await ctx.send(embed=sorry(apology=reason))
            return

        await self.send_embed_for_taxon(ctx, filtered_taxon)

    @inat.command()
    @checks.admin_or_permissions(manage_roles=True)
    async def projectadd(self, ctx, project_id: int, emoji: Union[str, discord.Emoji]):
        """Add user project for guild (mods only)."""
        config = self.config.guild(ctx.guild)
        user_projects = await config.user_projects()
        project_id_str = str(project_id)
        if project_id_str in user_projects:
            await ctx.send("iNat user project already known.")
            return

        user_projects[project_id_str] = str(emoji)
        await config.user_projects.set(user_projects)
        await ctx.send("iNat user project added.")

    @inat.command()
    @checks.admin_or_permissions(manage_roles=True)
    async def projectdel(self, ctx, project_id: int):
        """Remove user project for guild (mods only)."""
        config = self.config.guild(ctx.guild)
        user_projects = await config.user_projects()
        project_id_str = str(project_id)

        if project_id_str not in user_projects:
            await ctx.send("iNat user project not known.")
            return

        del user_projects[project_id_str]
        await config.user_projects.set(user_projects)
        await ctx.send("iNat user project removed.")

    @inat.group(invoke_without_command=True)
    async def user(self, ctx, *, who: QuotedContextMemberConverter):
        """Show user if their iNat id is known."""
        if not ctx.guild:
            return

        try:
            user = await self.user_table.get_user(who.member, refresh_cache=True)
        except LookupError as err:
            reason = err.args[0]
            await ctx.send(embed=sorry(apology=reason))
            return

        embed = make_embed(description=f"{who.member.mention} is {user.profile_link()}")
        user_projects = await self.config.guild(ctx.guild).user_projects() or []
        for project_id in user_projects:
            response = await self.api.get_projects(int(project_id), refresh_cache=True)
            user_project = UserProject.from_dict(response["results"][0])
            if user.user_id in user_project.observed_by_ids():
                response = await self.api.get_project_observers_stats(
                    project_id=project_id
                )
                stats = [
                    ObserverStats.from_dict(observer)
                    for observer in response["results"]
                ]
                if stats:
                    emoji = user_projects[project_id]
                    # FIXME: DRY!
                    obs_rank = next(
                        (
                            index + 1
                            for (index, d) in enumerate(stats)
                            if d.user_id == user.user_id
                        ),
                        None,
                    )
                    if obs_rank:
                        obs_cnt = stats[obs_rank - 1].observation_count
                        obs_pct = ceil(100 * (obs_rank / len(stats)))
                    else:
                        obs_rank = "unranked"
                        obs_cnt = "unknown"
                        obs_pct = "na"
                    response = await self.api.get_project_observers_stats(
                        project_id=project_id, order_by="species_count"
                    )
                    stats = [
                        ObserverStats.from_dict(observer)
                        for observer in response["results"]
                    ]
                    spp_rank = next(
                        (
                            index + 1
                            for (index, d) in enumerate(stats)
                            if d.user_id == user.user_id
                        ),
                        None,
                    )
                    if spp_rank:
                        spp_cnt = stats[spp_rank - 1].species_count
                        spp_pct = ceil(100 * (spp_rank / len(stats)))
                    else:
                        spp_rank = "unranked"
                        spp_cnt = "unknown"
                        spp_pct = "na"
                    fmt = f"{obs_cnt} (#{obs_rank}, {obs_pct}%) {spp_cnt} (#{spp_rank}, {spp_pct}%)"
                    embed.add_field(
                        name=f"{emoji} Obs# (rank,%) Spp# (rank,%)",
                        value=fmt,
                        inline=True,
                    )
        embed.add_field(name="Ids", value=user.identifications_count, inline=True)

        await ctx.send(embed=embed)

    @user.command(name="add")
    @checks.admin_or_permissions(manage_roles=True)
    async def user_add(self, ctx, discord_user: discord.User, inat_user):
        """Add user as an iNat user (mods only)."""
        config = self.config.user(discord_user)

        inat_user_id = await config.inat_user_id()
        known_all = await config.known_all()
        known_in = await config.known_in()
        if inat_user_id and known_all or ctx.guild.id in known_in:
            await ctx.send("iNat user already known.")
            return

        mat_link = re.search(PAT_USER_LINK, inat_user)
        match = mat_link and (mat_link["user_id"] or mat_link["login"])
        if match:
            user_query = match
        else:
            user_query = inat_user

        user = None
        response = await self.api.get_users(user_query, refresh_cache=True)
        if response and response["results"]:
            user = User.from_dict(response["results"][0])
            mat_login = user_query.lower()
            mat_id = int(user_query) if user_query.isnumeric() else None
            if not ((user.login == mat_login) or (user.user_id == mat_id)):
                user = None

        if not user:
            await ctx.send("iNat user not found.")
            return

        # We don't support registering one Discord user on different servers
        # to different iNat user IDs! Corrective action is: bot owner removes
        # the user (will be removed from all guilds) before they can be added
        # under the new iNat ID.
        if inat_user_id:
            if inat_user_id != user.user_id:
                await ctx.send(
                    "New iNat user id for user! Registration under old id must be removed first."
                )
                return
        else:
            await config.inat_user_id.set(user.user_id)

        known_in.append(ctx.guild.id)
        await config.known_in.set(known_in)

        await ctx.send(
            f"{discord_user.display_name} is added as {user.display_name()}."
        )

    @user.command(name="remove")
    @checks.admin_or_permissions(manage_roles=True)
    async def user_remove(self, ctx, discord_user: discord.User):
        """Remove user as an iNat user (mods only)."""
        config = self.config.user(discord_user)
        inat_user_id = await config.inat_user_id()
        known_in = await config.known_in()
        known_all = await config.known_all()
        if not inat_user_id or not (known_all or ctx.guild.id in known_in):
            await ctx.send("iNat user not known.")
            return
        # User can only be removed from servers where they were added:
        if ctx.guild.id in known_in:
            known_in.remove(ctx.guild.id)
            await config.known_in.set(known_in)
            if known_in:
                await ctx.send("iNat user removed from this server.")
            else:
                # Removal from last server removes all traces of the user:
                await config.inat_user_id.clear()
                await config.known_all.clear()
                await config.known_in.clear()
                await ctx.send("iNat user removed.")
        elif known_in and known_all:
            await ctx.send(
                "iNat user was added on another server and can only be removed there."
            )

    async def get_valid_user_config(self, ctx):
        """Get iNat user config known in this guild."""
        config = self.config.user(ctx.author)
        inat_user_id = await config.inat_user_id()
        known_in = await config.known_in()
        known_all = await config.known_all()
        if not (inat_user_id and known_all or ctx.guild.id in known_in):
            raise LookupError("Ask a moderator to add your iNat profile link.")
        return config

    async def user_show_settings(self, ctx, config, setting: str = "all"):
        """Show iNat user settings."""
        if setting not in ["all", "known", "home"]:
            await ctx.send(f"Unknown setting: {setting}")
            return
        if setting in ["all", "known"]:
            known_all = await config.known_all()
            await ctx.send(f"known: {known_all}")
        if setting in ["all", "home"]:
            home_id = await config.home()
            if home_id:
                try:
                    home = await self.place_table.get_place(ctx.guild, home_id)
                    await ctx.send(f"home: {home.display_name} (<{home.url}>)")
                except LookupError:
                    await ctx.send(f"Non-existent place ({home_id})")
            else:
                await ctx.send("home: none")

    @user.group(name="set", invoke_without_command=True)
    async def user_set(self, ctx, arg: str = None):
        """Show or set your iNat user settings.

        `[p]inat user set` shows all settings
        `[p]inat user set [name]` shows the named setting
        `[p]inat user set [name] [value]` set value of the named setting
        """
        if arg:
            await ctx.send(f"Unknown setting: {arg}")
            return
        try:
            config = await self.get_valid_user_config(ctx)
        except LookupError as err:
            await ctx.send(err)
            return

        await self.user_show_settings(ctx, config)

    @user_set.command(name="home")
    async def user_set_home(self, ctx, value: str = None):
        """Show or set your home iNat place.

        `[p]inat user set home` show your home place
        `[p]inat user set home clear` clear your home place
        `[p]inat user set home [place]` set your home place
        """
        try:
            config = await self.get_valid_user_config(ctx)
        except LookupError as err:
            await ctx.send(err)

        if value is not None:
            bot = self.bot.user.name
            if value.lower() in ["clear", "none"]:
                await config.home.clear()
                await ctx.send(f"{bot} no longer has a home place set for you.")
            else:
                try:
                    home = await self.place_table.get_place(ctx.guild, value)
                    await config.home.set(home.place_id)
                    await ctx.send(
                        f"{bot} will use {home.display_name} as your home place."
                    )
                except LookupError as err:
                    ctx.send(err)

        await self.user_show_settings(ctx, config, "home")

    @user_set.command(name="known")
    async def user_set_known(self, ctx, value: bool = None):
        """Show or set if your iNat user settings are known on other servers.

        `[p]inat user set known` show known on other servers (default: not known)
        `[p]inat user set known true` set known on other servers
        """
        try:
            config = await self.get_valid_user_config(ctx)
        except LookupError as err:
            await ctx.send(err)

        if value is not None:
            await config.known_all.set(value)

            bot = self.bot.user.name
            if value:
                await ctx.send(
                    f"{bot} will know your iNat settings when you join a server it is on."
                )
            else:
                await ctx.send(
                    f"{bot} will not know your iNat settings when you join a server it is on"
                    " until you have been added there."
                )

        await self.user_show_settings(ctx, config, "known")

    @user.command(name="list")
    @checks.admin_or_permissions(manage_roles=True)
    async def user_list(self, ctx):
        """List members with known iNat ids (mods only)."""
        if not ctx.guild:
            return

        # Avoid having to fully enumerate pages of discord/iNat user pairs
        # which would otherwise do expensive API calls if not in the cache
        # already just to get # of pages of member users:
        all_users = await self.config.all_users()
        config = self.config.guild(ctx.guild)
        user_projects = await config.user_projects()

        responses = [
            await self.api.get_projects(int(project_id)) for project_id in user_projects
        ]
        projects = [
            UserProject.from_dict(response["results"][0])
            for response in responses
            if response
        ]

        if not self.user_cache_init.get(ctx.guild.id):
            await self.api.get_observers_from_projects(user_projects.keys())
            self.user_cache_init[ctx.guild.id] = True

        def emojis(user_id: int):
            emojis = [
                user_projects[str(project.project_id)]
                for project in projects
                if user_id in project.observed_by_ids()
            ]
            return " ".join(emojis)

        # TODO: Support lazy loading of pages of users (issues noted in comments below).
        all_member_users = {
            key: value
            for (key, value) in all_users.items()
            if ctx.guild.get_member(key)
        }

        all_names = [
            f"{duser.mention} is {iuser.profile_link()} {emojis(iuser.user_id)}"
            async for (duser, iuser) in self.user_table.get_user_pairs(
                ctx.guild, all_member_users
            )
        ]

        pages = ["\n".join(filter(None, names)) for names in grouper(all_names, 10)]

        if pages:
            pages_len = len(pages)  # Causes enumeration (works against lazy load).
            embeds = [
                make_embed(
                    title=f"Discord iNat user list (page {index} of {pages_len})",
                    description=page,
                )
                for index, page in enumerate(pages, start=1)
            ]
            # menu() does not support lazy load of embeds iterator.
            await menu(ctx, embeds, DEFAULT_CONTROLS)
        else:
            await ctx.send(
                f"No iNat login ids are known. Add them with `{ctx.clean_prefix}inat user add`."
            )
