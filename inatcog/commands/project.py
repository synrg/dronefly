"""Module for project command group."""

from redbot.core import checks, commands
from redbot.core.commands import BadArgument
from redbot.core.utils.menus import menu, DEFAULT_CONTROLS

from inatcog.base_classes import WWW_BASE_URL
from inatcog.checks import known_inat_user
from inatcog.common import grouper
from inatcog.converters import ContextMemberConverter
from inatcog.embeds import apologize, make_embed
from inatcog.inat_embeds import INatEmbeds
from inatcog.interfaces import MixinMeta
from inatcog.places import RESERVED_PLACES


class CommandsProject(INatEmbeds, MixinMeta):
    """Mixin providing project command group."""

    @commands.group(invoke_without_command=True)
    async def project(self, ctx, *, query):
        """Show iNat project or abbreviation.

        **query** may contain:
        - *id#* of the iNat project
        - *words* in the iNat project name
        - *abbreviation* defined with `[p]project add`
        """
        try:
            project = await self.project_table.get_project(ctx.guild, query)
            await ctx.send(project.url)
        except LookupError as err:
            await ctx.send(err)

    @known_inat_user()
    @project.command(name="add")
    async def project_add(self, ctx, abbrev: str, project_number: int):
        """Add project abbreviation for server."""
        if not ctx.guild:
            return

        config = self.config.guild(ctx.guild)
        projects = await config.projects()
        abbrev_lowered = abbrev.lower()
        if abbrev_lowered in RESERVED_PLACES:
            await ctx.send(
                f"Project abbreviation '{abbrev_lowered}' cannot be added as it is reserved."
            )

        if abbrev_lowered in projects:
            url = f"{WWW_BASE_URL}/projects/{projects[abbrev_lowered]}"
            await ctx.send(
                f"Project abbreviation '{abbrev_lowered}' is already defined as: {url}"
            )
            return

        projects[abbrev_lowered] = project_number
        await config.projects.set(projects)
        await ctx.send("Project abbreviation added.")

    @project.command(name="list")
    @checks.bot_has_permissions(embed_links=True)
    async def project_list(self, ctx):
        """List projects with abbreviations on this server."""
        if not ctx.guild:
            return

        config = self.config.guild(ctx.guild)
        projects = await config.projects()
        result_pages = []
        for abbrev in projects:
            # Only lookup cached projects. Uncached projects will just be shown by number.
            proj_id = int(projects[abbrev])
            if proj_id in self.api.projects_cache:
                try:
                    project = await self.project_table.get_project(ctx.guild, proj_id)
                    proj_str = f"{abbrev}: [{project.title}]({project.url})"
                except LookupError:
                    proj_str = f"{abbrev}: {proj_id} not found."
            else:
                proj_str = f"{abbrev}: [{proj_id}]({WWW_BASE_URL}/projects/{proj_id})"
            result_pages.append(proj_str)
        pages = [
            "\n".join(filter(None, results)) for results in grouper(result_pages, 10)
        ]
        if pages:
            pages_len = len(pages)  # Causes enumeration (works against lazy load).
            embeds = [
                make_embed(
                    title=f"Project abbreviations (page {index} of {pages_len})",
                    description=page,
                )
                for index, page in enumerate(pages, start=1)
            ]
            # menu() does not support lazy load of embeds iterator.
            await menu(ctx, embeds, DEFAULT_CONTROLS)
        else:
            await apologize(ctx, "Nothing found")

    @known_inat_user()
    @project.command(name="remove")
    async def project_remove(self, ctx, abbrev: str):
        """Remove project abbreviation for server."""
        if not ctx.guild:
            return

        config = self.config.guild(ctx.guild)
        projects = await config.projects()
        abbrev_lowered = abbrev.lower()

        if abbrev_lowered not in projects:
            await ctx.send("Project abbreviation not defined.")
            return

        del projects[abbrev_lowered]
        await config.projects.set(projects)
        await ctx.send("Project abbreviation removed.")

    @project.command(name="stats")
    @checks.bot_has_permissions(embed_links=True)
    async def project_stats(self, ctx, project: str, *, user: str = "me"):
        """Show project stats for the named user.

        Observation & species count & rank of the user within the project
        are shown, as well as leaf taxa, which are not ranked. Leaf taxa
        are explained here:
        https://www.inaturalist.org/pages/how_inaturalist_counts_taxa
        """

        if project == "":
            await ctx.send_help()
        try:
            proj = await self.project_table.get_project(ctx.guild, project)
        except LookupError as err:
            await ctx.send(err)
            return

        try:
            ctx_member = await ContextMemberConverter.convert(ctx, user)
            member = ctx_member.member
            user = await self.user_table.get_user(member)
        except (BadArgument, LookupError) as err:
            await ctx.send(err)
            return

        embed = await self.make_stats_embed(member, user, proj)
        await ctx.send(embed=embed)
