"""A cog for using the iNaturalist platform."""
from abc import ABC
import re
import asyncio
import inflect
from redbot.core import commands, Config
from .api import INatAPI
from .base_classes import PAT_OBS_LINK, User
from .checks import known_inat_user
from .commands.inat import CommandsInat
from .commands.last import CommandsLast
from .commands.obs import CommandsObs
from .commands.place import CommandsPlace
from .commands.project import CommandsProject
from .commands.search import CommandsSearch
from .commands.user import CommandsUser
from .commands.taxon import CommandsTaxon
from .converters import ContextMemberConverter
from .embeds import sorry
from .obs import get_obs_fields
from .obs_query import INatObsQuery
from .places import INatPlaceTable
from .projects import INatProjectTable
from .listeners import Listeners
from .search import INatSiteSearch
from .taxa import PAT_TAXON_LINK
from .taxon_query import INatTaxonQuery
from .users import INatUserTable

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
class INatCog(
    Listeners,
    commands.Cog,
    CommandsInat,
    CommandsLast,
    CommandsObs,
    CommandsPlace,
    CommandsProject,
    CommandsSearch,
    CommandsTaxon,
    CommandsUser,
    name="iNat",
    metaclass=CompositeMetaClass,
):
    """Commands provided by `inatcog`."""

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, identifier=1607)
        self.api = INatAPI()
        self.p = inflect.engine()  # pylint: disable=invalid-name
        self.obs_query = INatObsQuery(self)
        self.taxon_query = INatTaxonQuery(self)
        self.user_table = INatUserTable(self)
        self.place_table = INatPlaceTable(self)
        self.project_table = INatProjectTable(self)
        self.site_search = INatSiteSearch(self)
        self.user_cache_init = {}
        self.reaction_locks = {}
        self.predicate_locks = {}

        self.config.register_global(home=97394, schema_version=1)  # North America
        self.config.register_guild(
            autoobs=False,
            dot_taxon=False,
            active_role=None,
            bot_prefixes=[],
            inactive_role=None,
            user_projects={},
            places={},
            home=97394,  # North America
            projects={},
            project_emojis={},
        )
        self.config.register_channel(autoobs=None, dot_taxon=None)
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
            # re-register each user, or the user must `[p]user set known
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

    @commands.command()
    async def link(self, ctx, *, query):
        """Show summary for iNaturalist link.

        e.g.
        ```
        [p]link https://inaturalist.org/observations/#
           -> an embed summarizing the observation link
        ```
        """
        mat = re.search(PAT_OBS_LINK, query)
        if mat:
            obs_id = int(mat["obs_id"])
            url = mat["url"]

            home = await self.get_home(ctx)
            results = (
                await self.api.get_observations(
                    obs_id, include_new_projects=1, preferred_place_id=home
                )
            )["results"]
            obs = get_obs_fields(results[0]) if results else None
            await ctx.send(embed=await self.make_obs_embed(ctx.guild, obs, url))
            if obs and obs.sounds:
                await self.maybe_send_sound_url(ctx.channel, obs.sounds[0])
            return

        mat = re.search(PAT_TAXON_LINK, query)
        if mat:
            await self.taxon(ctx, query=mat["taxon_id"])
            return

        await ctx.send(embed=sorry())

    @commands.command()
    async def map(self, ctx, *, taxa_list):
        """Show range map for a list of one or more taxa.

        **Examples:**
        ```
        [p]map polar bear
        [p]map 24255,24267
        [p]map boreal chorus frog,western chorus frog
        ```
        See `[p]help taxon` for help specifying taxa.
        """

        if not taxa_list:
            await ctx.send_help()
            return

        try:
            taxa = await self.taxon_query.query_taxa(ctx, taxa_list)
        except LookupError as err:
            reason = err.args[0]
            await ctx.send(embed=sorry(apology=reason))
            return

        await ctx.send(embed=await self.make_map_embed(taxa))

    @commands.command()
    async def iuser(self, ctx, *, login: str):
        """Show iNat user matching login.

        Examples:

        `[p]iuser kueda`
        """
        if not ctx.guild:
            return

        found = None
        response = await self.api.get_users(login, refresh_cache=True)
        if response and response["results"]:
            found = next(
                (
                    result
                    for result in response["results"]
                    if login in (str(result["id"]), result["login"])
                ),
                None,
            )
        if not found:
            await ctx.send(embed=sorry(apology="Not found"))
            return

        inat_user = User.from_dict(found)
        await ctx.send(inat_user.profile_url())

    @commands.command()
    @known_inat_user()
    async def me(self, ctx):  # pylint: disable=invalid-name
        """Show your iNat info & stats for this server."""
        member = await ContextMemberConverter.convert(ctx, "me")
        await self.user(ctx, who=member)

    @commands.group(invoke_without_command=True)
    @known_inat_user()
    async def my(self, ctx, *, project: str):  # pylint: disable=invalid-name
        """Show your observations, species, & ranks for an iNat project."""
        await self.project_stats(ctx, project, user="me")

    @my.command(name="inatyear", invoke_without_command=True)
    @known_inat_user()
    async def my_inatyear(self, ctx, year: int = None):
        """Display the URL for your iNat year graphs.

        Where `year` is a valid year on or after 1950."""
        await self.user_inatyear(ctx, user="me", year=year)

    @commands.command()
    async def rank(
        self, ctx, project: str, *, user: str
    ):  # pylint: disable=invalid-name
        """Show observations, species, & ranks in an iNat project for a user."""
        await self.project_stats(ctx, project, user=user)
