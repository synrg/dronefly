"""A cog for using the iNaturalist platform."""
from abc import ABC
import re
import asyncio
import inflect
from redbot.core import commands, Config
from .api import INatAPI
from .commands.inat import CommandsInat
from .commands.last import CommandsLast
from .commands.obs import CommandsObs
from .commands.place import CommandsPlace
from .commands.project import CommandsProject
from .commands.search import CommandsSearch
from .commands.taxon import CommandsTaxon
from .commands.user import CommandsUser
from .obs_query import INatObsQuery
from .places import INatPlaceTable
from .projects import INatProjectTable
from .listeners import Listeners
from .search import INatSiteSearch
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
