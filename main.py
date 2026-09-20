import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import asyncio
import time
from datetime import timedelta
from collections import defaultdict, deque

TOKEN = "YOUR_BOT_TOKEN"
GUILD_ID = YOUR_SERVR_ID
DB = "bot.db"

AUTO_SPAM_LIMIT = 6
AUTO_SPAM_WINDOW = 8
AUTO_SPAM_TIMEOUT = 60

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None
)

tree = bot.tree

spam_tracker = defaultdict(lambda: deque(maxlen=20))


async def db_execute(query, params=(), fetch=False, fetchone=False):
    async with aiosqlite.connect(DB) as db:
        cursor = await db.execute(query, params)

        if fetchone:
            result = await cursor.fetchone()
        elif fetch:
            result = await cursor.fetchall()
        else:
            result = None

        await db.commit()
        return result


async def init_db():
    await db_execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            logs_channel INTEGER,
            member_role INTEGER,
            ticket_category INTEGER,
            ticket_panel_channel INTEGER,
            verification_channel INTEGER
        )
    """)

    await db_execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            created_at INTEGER
        )
    """)


async def get_config(guild_id):
    row = await db_execute(
        "SELECT * FROM guild_config WHERE guild_id = ?",
        (guild_id,),
        fetchone=True
    )

    if row is None:
        await db_execute(
            "INSERT INTO guild_config (guild_id) VALUES (?)",
            (guild_id,)
        )

        row = await db_execute(
            "SELECT * FROM guild_config WHERE guild_id = ?",
            (guild_id,),
            fetchone=True
        )

    return {
        "guild_id": row[0],
        "logs_channel": row[1],
        "member_role": row[2],
        "ticket_category": row[3],
        "ticket_panel_channel": row[4],
        "verification_channel": row[5]
    }


async def update_config(guild_id, column, value):
    allowed = {
        "logs_channel",
        "member_role",
        "ticket_category",
        "ticket_panel_channel",
        "verification_channel"
    }

    if column not in allowed:
        return

    await get_config(guild_id)

    await db_execute(
        f"UPDATE guild_config SET {column} = ? WHERE guild_id = ?",
        (value, guild_id)
    )


def clean_text(text, limit=1000):
    if not text:
        return "No content"

    text = text.replace("`", "'")

    if len(text) > limit:
        return text[:limit - 3] + "..."

    return text


async def send_log(
    guild,
    title,
    description,
    color=discord.Color.blurple()
):
    config = await get_config(guild.id)

    channel_id = config["logs_channel"]

    if not channel_id:
        return

    channel = guild.get_channel(channel_id)

    if not channel:
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow()
    )

    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


def admin_check():
    async def predicate(interaction):
        if interaction.guild is None:
            return False

        if interaction.user.guild_permissions.administrator:
            return True

        await interaction.response.send_message(
            "❌ You need Administrator permissions to use this command.",
            ephemeral=True
        )

        return False

    return app_commands.check(predicate)


def mod_check():
    async def predicate(interaction):
        if interaction.guild is None:
            return False

        permissions = interaction.user.guild_permissions

        if (
            permissions.administrator
            or permissions.manage_messages
            or permissions.moderate_members
            or permissions.kick_members
            or permissions.ban_members
        ):
            return True

        await interaction.response.send_message(
            "❌ You don't have permission to use this command.",
            ephemeral=True
        )

        return False

    return app_commands.check(predicate)


def can_moderate(interaction, member):
    if member == interaction.guild.owner:
        return False

    if interaction.user != interaction.guild.owner:
        if member.top_role >= interaction.user.top_role:
            return False

    if interaction.guild.me:
        if member.top_role >= interaction.guild.me.top_role:
            return False

    return True


def get_ticket_owner(channel):
    if not channel.topic:
        return None

    if not channel.topic.startswith("ticket_owner:"):
        return None

    try:
        return int(channel.topic.split(":")[1])
    except ValueError:
        return None


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create Ticket",
        style=discord.ButtonStyle.blurple,
        emoji="🎫",
        custom_id="ticket_create"
    )
    async def create_ticket(self, interaction, button):
        guild = interaction.guild
        user = interaction.user

        config = await get_config(guild.id)
        category_id = config["ticket_category"]

        if not category_id:
            await interaction.response.send_message(
                "❌ Tickets haven't been set up yet.",
                ephemeral=True
            )
            return

        category = guild.get_channel(category_id)

        if not category:
            await interaction.response.send_message(
                "❌ The ticket category no longer exists.",
                ephemeral=True
            )
            return

        for channel in category.channels:
            if get_ticket_owner(channel) == user.id:
                await interaction.response.send_message(
                    f"❌ You already have a ticket: {channel.mention}",
                    ephemeral=True
                )
                return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False
            ),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True
            )
        }

        for role in guild.roles:
            if (
                role.permissions.manage_channels
                or role.permissions.administrator
            ):
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                )

        channel = await guild.create_text_channel(
            f"ticket-{user.id}",
            category=category,
            overwrites=overwrites,
            topic=f"ticket_owner:{user.id}"
        )

        embed = discord.Embed(
            title="🎫 Support Ticket",
            description=(
                f"Hello {user.mention}!\n\n"
                "Please explain your issue and a staff member will help you.\n\n"
                "Use the button below to close the ticket."
            ),
            color=discord.Color.blurple()
        )

        await channel.send(
            content=user.mention,
            embed=embed,
            view=TicketControlView()
        )

        await interaction.response.send_message(
            f"✅ Your ticket has been created: {channel.mention}",
            ephemeral=True
        )

        await send_log(
            guild,
            "🎫 Ticket Created",
            f"**User:** {user.mention}\n"
            f"**Channel:** {channel.mention}",
            discord.Color.green()
        )


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.red,
        emoji="🔒",
        custom_id="ticket_close"
    )
    async def close_ticket(self, interaction, button):
        channel = interaction.channel
        guild = interaction.guild

        owner_id = get_ticket_owner(channel)

        is_staff = (
            interaction.user.guild_permissions.administrator
            or interaction.user.guild_permissions.manage_channels
        )

        if owner_id != interaction.user.id and not is_staff:
            await interaction.response.send_message(
                "❌ You cannot close this ticket.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🔒 Closing ticket..."
        )

        await send_log(
            guild,
            "🔒 Ticket Closed",
            f"**Channel:** {channel.name}\n"
            f"**Closed by:** {interaction.user.mention}",
            discord.Color.red()
        )

        await asyncio.sleep(2)

        try:
            await channel.delete()
        except discord.HTTPException:
            pass


class VerificationModal(discord.ui.Modal, title="Verification"):
    answer = discord.ui.TextInput(
        label="Type VERIFY",
        placeholder="Type VERIFY",
        required=True,
        max_length=20
    )

    async def on_submit(self, interaction):
        if self.answer.value.strip().lower() != "verify":
            await interaction.response.send_message(
                "❌ Verification failed. Type `VERIFY` exactly.",
                ephemeral=True
            )
            return

        config = await get_config(interaction.guild.id)
        role_id = config["member_role"]

        if not role_id:
            await interaction.response.send_message(
                "❌ The member role hasn't been configured.",
                ephemeral=True
            )
            return

        role = interaction.guild.get_role(role_id)

        if not role:
            await interaction.response.send_message(
                "❌ The configured member role no longer exists.",
                ephemeral=True
            )
            return

        if interaction.guild.me and role >= interaction.guild.me.top_role:
            await interaction.response.send_message(
                "❌ I cannot give you this role because it is above my highest role.",
                ephemeral=True
            )
            return

        try:
            await interaction.user.add_roles(
                role,
                reason="Verification"
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ I couldn't give you the member role.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "✅ You have been verified!",
            ephemeral=True
        )

        await send_log(
            interaction.guild,
            "✅ Member Verified",
            f"**Member:** {interaction.user.mention}\n"
            f"**Role:** {role.mention}",
            discord.Color.green()
        )


class VerificationStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify",
        style=discord.ButtonStyle.green,
        emoji="✅",
        custom_id="verification_start"
    )
    async def verify(self, interaction, button):
        await interaction.response.send_modal(
            VerificationModal()
        )


@tree.command(
    name="setup-ticket",
    description="Set up a ticket panel in this channel"
)
@admin_check()
async def setup_ticket(interaction):
    guild = interaction.guild
    channel = interaction.channel

    category = discord.utils.get(
        guild.categories,
        name="Tickets"
    )

    if category is None:
        category = await guild.create_category(
            "Tickets"
        )

    embed = discord.Embed(
        title="🎫 Support Tickets",
        description=(
            "Need help?\n\n"
            "Click the button below to create a private ticket.\n\n"
            "A staff member will assist you as soon as possible."
        ),
        color=discord.Color.blurple()
    )

    await channel.send(
        embed=embed,
        view=TicketView()
    )

    await update_config(
        guild.id,
        "ticket_category",
        category.id
    )

    await update_config(
        guild.id,
        "ticket_panel_channel",
        channel.id
    )

    await interaction.response.send_message(
        f"✅ Ticket panel created in {channel.mention}.",
        ephemeral=True
    )

    await send_log(
        guild,
        "⚙️ Ticket Panel Created",
        f"**Created by:** {interaction.user.mention}\n"
        f"**Channel:** {channel.mention}\n"
        f"**Category:** {category.name}",
        discord.Color.blue()
    )


@tree.command(
    name="setup-verification",
    description="Set up the verification system"
)
@admin_check()
async def setup_verification(interaction):
    guild = interaction.guild

    channel = discord.utils.get(
        guild.text_channels,
        name="verify"
    )

    if channel is None:
        channel = await guild.create_text_channel(
            "verify"
        )

    embed = discord.Embed(
        title="✅ Verification",
        description=(
            "Welcome!\n\n"
            "Click the button below and type `VERIFY` "
            "to receive the member role."
        ),
        color=discord.Color.green()
    )

    await channel.send(
        embed=embed,
        view=VerificationStartView()
    )

    await update_config(
        guild.id,
        "verification_channel",
        channel.id
    )

    await interaction.response.send_message(
        f"✅ Verification system created in {channel.mention}.",
        ephemeral=True
    )

    await send_log(
        guild,
        "⚙️ Verification Setup",
        f"**Set up by:** {interaction.user.mention}\n"
        f"**Channel:** {channel.mention}",
        discord.Color.green()
    )


@tree.command(
    name="setmemberrole",
    description="Set the role verified members receive"
)
@admin_check()
@app_commands.describe(
    role="The member role"
)
async def setmemberrole(
    interaction,
    role: discord.Role
):
    if role.is_default():
        await interaction.response.send_message(
            "❌ You cannot use @everyone.",
            ephemeral=True
        )
        return

    if interaction.guild.me and role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            "❌ This role is above my highest role.",
            ephemeral=True
        )
        return

    await update_config(
        interaction.guild.id,
        "member_role",
        role.id
    )

    await interaction.response.send_message(
        f"✅ Verified members will receive {role.mention}.",
        ephemeral=True
    )

    await send_log(
        interaction.guild,
        "⚙️ Member Role Changed",
        f"**Role:** {role.mention}\n"
        f"**Changed by:** {interaction.user.mention}",
        discord.Color.blue()
    )


@tree.command(
    name="setuplogs",
    description="Set this channel as the logs channel"
)
@admin_check()
async def setuplogs(interaction):
    await update_config(
        interaction.guild.id,
        "logs_channel",
        interaction.channel.id
    )

    await interaction.response.send_message(
        "✅ This channel is now the logs channel.",
        ephemeral=True
    )

    await send_log(
        interaction.guild,
        "📋 Logs Setup",
        f"**Configured by:** {interaction.user.mention}",
        discord.Color.blue()
    )


ticket_group = app_commands.Group(
    name="ticket",
    description="Ticket management commands"
)


@ticket_group.command(
    name="close",
    description="Close the current ticket"
)
@mod_check()
async def ticket_close(interaction):
    channel = interaction.channel

    if get_ticket_owner(channel) is None:
        await interaction.response.send_message(
            "❌ This is not a ticket channel.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "🔒 Closing ticket..."
    )

    await send_log(
        interaction.guild,
        "🔒 Ticket Closed",
        f"**Channel:** {channel.name}\n"
        f"**Closed by:** {interaction.user.mention}",
        discord.Color.red()
    )

    await asyncio.sleep(2)

    try:
        await channel.delete()
    except discord.HTTPException:
        pass


@ticket_group.command(
    name="add",
    description="Add a member to the ticket"
)
@mod_check()
@app_commands.describe(
    member="Member to add"
)
async def ticket_add(
    interaction,
    member: discord.Member
):
    if get_ticket_owner(interaction.channel) is None:
        await interaction.response.send_message(
            "❌ This is not a ticket channel.",
            ephemeral=True
        )
        return

    await interaction.channel.set_permissions(
        member,
        view_channel=True,
        send_messages=True,
        read_message_history=True
    )

    await interaction.response.send_message(
        f"✅ Added {member.mention} to the ticket."
    )

    await send_log(
        interaction.guild,
        "🎫 Member Added To Ticket",
        f"**Member:** {member.mention}\n"
        f"**Channel:** {interaction.channel.mention}\n"
        f"**By:** {interaction.user.mention}",
        discord.Color.green()
    )


@ticket_group.command(
    name="remove",
    description="Remove a member from the ticket"
)
@mod_check()
@app_commands.describe(
    member="Member to remove"
)
async def ticket_remove(
    interaction,
    member: discord.Member
):
    if get_ticket_owner(interaction.channel) is None:
        await interaction.response.send_message(
            "❌ This is not a ticket channel.",
            ephemeral=True
        )
        return

    owner_id = get_ticket_owner(
        interaction.channel
    )

    if member.id == owner_id:
        await interaction.response.send_message(
            "❌ You cannot remove the ticket owner.",
            ephemeral=True
        )
        return

    await interaction.channel.set_permissions(
        member,
        overwrite=None
    )

    await interaction.response.send_message(
        f"✅ Removed {member.mention} from the ticket."
    )

    await send_log(
        interaction.guild,
        "🎫 Member Removed From Ticket",
        f"**Member:** {member.mention}\n"
        f"**Channel:** {interaction.channel.mention}\n"
        f"**By:** {interaction.user.mention}",
        discord.Color.orange()
    )


@ticket_group.command(
    name="rename",
    description="Rename the current ticket"
)
@mod_check()
@app_commands.describe(
    name="New ticket name"
)
async def ticket_rename(
    interaction,
    name: str
):
    if get_ticket_owner(interaction.channel) is None:
        await interaction.response.send_message(
            "❌ This is not a ticket channel.",
            ephemeral=True
        )
        return

    clean_name = "".join(
        character
        for character in name.lower()
        if character.isalnum() or character == "-"
    )

    if not clean_name:
        await interaction.response.send_message(
            "❌ Invalid channel name.",
            ephemeral=True
        )
        return

    old_name = interaction.channel.name

    await interaction.channel.edit(
        name=clean_name
    )

    await interaction.response.send_message(
        f"✅ Ticket renamed to `{clean_name}`."
    )

    await send_log(
        interaction.guild,
        "🎫 Ticket Renamed",
        f"**Old:** `{old_name}`\n"
        f"**New:** `{clean_name}`\n"
        f"**By:** {interaction.user.mention}",
        discord.Color.blue()
    )


tree.add_command(ticket_group)


@tree.command(
    name="warn",
    description="Warn a member"
)
@mod_check()
@app_commands.describe(
    member="Member to warn",
    reason="Reason for the warning"
)
async def warn(
    interaction,
    member: discord.Member,
    reason: str
):
    if not can_moderate(
        interaction,
        member
    ):
        await interaction.response.send_message(
            "❌ You cannot moderate this member.",
            ephemeral=True
        )
        return

    await db_execute(
        """
        INSERT INTO warnings
        (guild_id, user_id, moderator_id, reason, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            interaction.guild.id,
            member.id,
            interaction.user.id,
            reason,
            int(time.time())
        )
    )

    await interaction.response.send_message(
        f"⚠️ {member.mention} has been warned.\n"
        f"**Reason:** {reason}"
    )

    await send_log(
        interaction.guild,
        "⚠️ Member Warned",
        f"**Member:** {member.mention}\n"
        f"**Moderator:** {interaction.user.mention}\n"
        f"**Reason:** {reason}",
        discord.Color.orange()
    )


@tree.command(
    name="warnings",
    description="View a member's warnings"
)
@mod_check()
@app_commands.describe(
    member="Member"
)
async def warnings(
    interaction,
    member: discord.Member
):
    rows = await db_execute(
        """
        SELECT moderator_id, reason, created_at
        FROM warnings
        WHERE guild_id = ? AND user_id = ?
        ORDER BY created_at DESC
        """,
        (
            interaction.guild.id,
            member.id
        ),
        fetch=True
    )

    if not rows:
        await interaction.response.send_message(
            f"✅ {member.mention} has no warnings.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"Warnings — {member}",
        color=discord.Color.orange()
    )

    for index, row in enumerate(
        rows,
        start=1
    ):
        moderator = interaction.guild.get_member(
            row[0]
        )

        moderator_name = (
            moderator.mention
            if moderator
            else str(row[0])
        )

        embed.add_field(
            name=f"Warning #{index}",
            value=(
                f"**Reason:** {clean_text(row[1], 500)}\n"
                f"**Moderator:** {moderator_name}\n"
                f"**Date:** <t:{row[2]}:R>"
            ),
            inline=False
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


@tree.command(
    name="clearwarnings",
    description="Clear all warnings"
)
@mod_check()
@app_commands.describe(
    member="Member"
)
async def clearwarnings(
    interaction,
    member: discord.Member
):
    await db_execute(
        """
        DELETE FROM warnings
        WHERE guild_id = ? AND user_id = ?
        """,
        (
            interaction.guild.id,
            member.id
        )
    )

    await interaction.response.send_message(
        f"✅ Cleared all warnings for {member.mention}."
    )

    await send_log(
        interaction.guild,
        "🧹 Warnings Cleared",
        f"**Member:** {member.mention}\n"
        f"**Moderator:** {interaction.user.mention}",
        discord.Color.green()
    )


@tree.command(
    name="timeout",
    description="Timeout a member"
)
@mod_check()
@app_commands.describe(
    member="Member",
    minutes="Timeout duration in minutes",
    reason="Reason"
)
async def timeout(
    interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str
):
    if not can_moderate(
        interaction,
        member
    ):
        await interaction.response.send_message(
            "❌ You cannot moderate this member.",
            ephemeral=True
        )
        return

    await member.timeout(
        timedelta(minutes=minutes),
        reason=reason
    )

    await interaction.response.send_message(
        f"🔇 {member.mention} has been timed out "
        f"for {minutes} minute(s).\n"
        f"**Reason:** {reason}"
    )

    await send_log(
        interaction.guild,
        "🔇 Member Timed Out",
        f"**Member:** {member.mention}\n"
        f"**Duration:** {minutes} minutes\n"
        f"**Moderator:** {interaction.user.mention}\n"
        f"**Reason:** {reason}",
        discord.Color.orange()
    )


@tree.command(
    name="untimeout",
    description="Remove a timeout"
)
@mod_check()
@app_commands.describe(
    member="Member"
)
async def untimeout(
    interaction,
    member: discord.Member
):
    if not can_moderate(
        interaction,
        member
    ):
        await interaction.response.send_message(
            "❌ You cannot moderate this member.",
            ephemeral=True
        )
        return

    await member.timeout(
        None,
        reason=f"Timeout removed by {interaction.user}"
    )

    await interaction.response.send_message(
        f"🔊 {member.mention} is no longer timed out."
    )

    await send_log(
        interaction.guild,
        "🔊 Timeout Removed",
        f"**Member:** {member.mention}\n"
        f"**Moderator:** {interaction.user.mention}",
        discord.Color.green()
    )


@tree.command(
    name="kick",
    description="Kick a member"
)
@mod_check()
@app_commands.describe(
    member="Member",
    reason="Reason"
)
async def kick(
    interaction,
    member: discord.Member,
    reason: str
):
    if not can_moderate(
        interaction,
        member
    ):
        await interaction.response.send_message(
            "❌ You cannot kick this member.",
            ephemeral=True
        )
        return

    await member.kick(
        reason=reason
    )

    await interaction.response.send_message(
        f"👢 {member} has been kicked.\n"
        f"**Reason:** {reason}"
    )

    await send_log(
        interaction.guild,
        "👢 Member Kicked",
        f"**Member:** {member}\n"
        f"**Moderator:** {interaction.user.mention}\n"
        f"**Reason:** {reason}",
        discord.Color.red()
    )


@tree.command(
    name="ban",
    description="Ban a member"
)
@mod_check()
@app_commands.describe(
    member="Member",
    reason="Reason"
)
async def ban(
    interaction,
    member: discord.Member,
    reason: str
):
    if not can_moderate(
        interaction,
        member
    ):
        await interaction.response.send_message(
            "❌ You cannot ban this member.",
            ephemeral=True
        )
        return

    await member.ban(
        reason=reason
    )

    await interaction.response.send_message(
        f"🔨 {member} has been banned.\n"
        f"**Reason:** {reason}"
    )

    await send_log(
        interaction.guild,
        "🔨 Member Banned",
        f"**Member:** {member}\n"
        f"**Moderator:** {interaction.user.mention}\n"
        f"**Reason:** {reason}",
        discord.Color.red()
    )


@tree.command(
    name="unban",
    description="Unban a user"
)
@mod_check()
@app_commands.describe(
    user_id="User ID",
    reason="Reason"
)
async def unban(
    interaction,
    user_id: str,
    reason: str
):
    try:
        user_id_int = int(user_id)
    except ValueError:
        await interaction.response.send_message(
            "❌ Invalid user ID.",
            ephemeral=True
        )
        return

    try:
        user = await bot.fetch_user(
            user_id_int
        )

        await interaction.guild.unban(
            user,
            reason=reason
        )

    except discord.NotFound:
        await interaction.response.send_message(
            "❌ This user is not banned or could not be found.",
            ephemeral=True
        )
        return

    except discord.HTTPException:
        await interaction.response.send_message(
            "❌ I couldn't unban this user.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"🔓 {user} has been unbanned.\n"
        f"**Reason:** {reason}"
    )

    await send_log(
        interaction.guild,
        "🔓 User Unbanned",
        f"**User:** {user}\n"
        f"**Moderator:** {interaction.user.mention}\n"
        f"**Reason:** {reason}",
        discord.Color.green()
    )


@tree.command(
    name="purge",
    description="Delete messages"
)
@mod_check()
@app_commands.describe(
    amount="Number of messages to delete"
)
async def purge(
    interaction,
    amount: app_commands.Range[int, 1, 100]
):
    await interaction.response.defer(
        ephemeral=True
    )

    deleted = await interaction.channel.purge(
        limit=amount
    )

    await interaction.followup.send(
        f"🧹 Deleted {len(deleted)} message(s).",
        ephemeral=True
    )

    await send_log(
        interaction.guild,
        "🧹 Messages Purged",
        f"**Channel:** {interaction.channel.mention}\n"
        f"**Amount:** {len(deleted)}\n"
        f"**Moderator:** {interaction.user.mention}",
        discord.Color.orange()
    )


@tree.command(
    name="lock",
    description="Lock the current channel"
)
@mod_check()
async def lock(interaction):
    await interaction.channel.set_permissions(
        interaction.guild.default_role,
        send_messages=False
    )

    await interaction.response.send_message(
        "🔒 This channel has been locked."
    )

    await send_log(
        interaction.guild,
        "🔒 Channel Locked",
        f"**Channel:** {interaction.channel.mention}\n"
        f"**Moderator:** {interaction.user.mention}",
        discord.Color.red()
    )


@tree.command(
    name="unlock",
    description="Unlock the current channel"
)
@mod_check()
async def unlock(interaction):
    await interaction.channel.set_permissions(
        interaction.guild.default_role,
        send_messages=None
    )

    await interaction.response.send_message(
        "🔓 This channel has been unlocked."
    )

    await send_log(
        interaction.guild,
        "🔓 Channel Unlocked",
        f"**Channel:** {interaction.channel.mention}\n"
        f"**Moderator:** {interaction.user.mention}",
        discord.Color.green()
    )


@tree.command(
    name="slowmode",
    description="Set channel slowmode"
)
@mod_check()
@app_commands.describe(
    seconds="Slowmode duration in seconds"
)
async def slowmode(
    interaction,
    seconds: app_commands.Range[int, 0, 21600]
):
    await interaction.channel.edit(
        slowmode_delay=seconds
    )

    await interaction.response.send_message(
        f"🐌 Slowmode set to {seconds} second(s)."
    )

    await send_log(
        interaction.guild,
        "🐌 Slowmode Changed",
        f"**Channel:** {interaction.channel.mention}\n"
        f"**Seconds:** {seconds}\n"
        f"**Moderator:** {interaction.user.mention}",
        discord.Color.blue()
    )


@tree.command(
    name="userinfo",
    description="Show information about a member"
)
@app_commands.describe(
    member="Member"
)
async def userinfo(
    interaction,
    member: discord.Member
):
    roles = [
        role.mention
        for role in member.roles[1:]
    ]

    embed = discord.Embed(
        title=f"User Info — {member}",
        color=member.color
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    embed.add_field(
        name="User",
        value=f"{member.mention}\n`{member.id}`",
        inline=False
    )

    embed.add_field(
        name="Joined",
        value=(
            discord.utils.format_dt(
                member.joined_at,
                "F"
            )
            if member.joined_at
            else "Unknown"
        ),
        inline=False
    )

    embed.add_field(
        name="Created",
        value=discord.utils.format_dt(
            member.created_at,
            "F"
        ),
        inline=False
    )

    embed.add_field(
        name="Roles",
        value=(
            " ".join(roles[-15:])
            if roles
            else "None"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed
    )


@tree.command(
    name="serverinfo",
    description="Show server information"
)
async def serverinfo(interaction):
    guild = interaction.guild

    embed = discord.Embed(
        title=f"Server Info — {guild.name}",
        color=discord.Color.blurple()
    )

    if guild.icon:
        embed.set_thumbnail(
            url=guild.icon.url
        )

    embed.add_field(
        name="Owner",
        value=(
            guild.owner.mention
            if guild.owner
            else "Unknown"
        )
    )

    embed.add_field(
        name="Members",
        value=str(guild.member_count)
    )

    embed.add_field(
        name="Channels",
        value=str(len(guild.channels))
    )

    embed.add_field(
        name="Roles",
        value=str(len(guild.roles))
    )

    embed.add_field(
        name="Created",
        value=discord.utils.format_dt(
            guild.created_at,
            "F"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed
    )


@tree.command(
    name="help",
    description="Show the bot command tutorial"
)
async def help_command(interaction):
    embed = discord.Embed(
        title="📖 Bot Commands",
        description="Here is a simple tutorial for the bot.",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="⚙️ Setup",
        value=(
            "`/setup-ticket` — Create a ticket panel in this channel\n"
            "`/setup-verification` — Create the verification system\n"
            "`/setmemberrole @role` — Set the verified member role\n"
            "`/setuplogs` — Set this channel as the logs channel"
        ),
        inline=False
    )

    embed.add_field(
        name="🎫 Tickets",
        value=(
            "`/ticket close` — Close the current ticket\n"
            "`/ticket add @member` — Add someone to a ticket\n"
            "`/ticket remove @member` — Remove someone from a ticket\n"
            "`/ticket rename name` — Rename a ticket"
        ),
        inline=False
    )

    embed.add_field(
        name="🛡️ Moderation",
        value=(
            "`/warn @member reason` — Warn a member\n"
            "`/warnings @member` — View warnings\n"
            "`/clearwarnings @member` — Clear warnings\n"
            "`/timeout @member minutes reason` — Timeout a member\n"
            "`/untimeout @member` — Remove a timeout\n"
            "`/kick @member reason` — Kick a member\n"
            "`/ban @member reason` — Ban a member\n"
            "`/unban user_id reason` — Unban a user\n"
            "`/purge amount` — Delete messages\n"
            "`/lock` — Lock the channel\n"
            "`/unlock` — Unlock the channel\n"
            "`/slowmode seconds` — Set slowmode"
        ),
        inline=False
    )

    embed.add_field(
        name="ℹ️ Information",
        value=(
            "`/userinfo @member` — View member information\n"
            "`/serverinfo` — View server information\n"
            "`/help` — Show this tutorial"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=embed
    )


@bot.event
async def setup_hook():
    await init_db()

    bot.add_view(TicketView())
    bot.add_view(TicketControlView())
    bot.add_view(VerificationStartView())

    guild = discord.Object(id=GUILD_ID)

    # Sync ONLY to the guild. Do not touch global commands here.
    # Global duplicates must be cleared once using the one-off
    # script described in the reply text — do not do it in this hook,
    # or it will wipe the local tree and break everything.
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)

    print("Slash commands synced to guild successfully.")


@bot.event
async def on_ready():
    print(
        f"Logged in as {bot.user} ({bot.user.id})"
    )

    print(
        f"Connected to {len(bot.guilds)} server(s)"
    )


@bot.event
async def on_member_join(member):
    await send_log(
        member.guild,
        "📥 Member Joined",
        f"**Member:** {member.mention}\n"
        f"**ID:** `{member.id}`",
        discord.Color.green()
    )


@bot.event
async def on_member_remove(member):
    await send_log(
        member.guild,
        "📤 Member Left",
        f"**Member:** {member}\n"
        f"**ID:** `{member.id}`",
        discord.Color.orange()
    )


@bot.event
async def on_member_ban(guild, user):
    await send_log(
        guild,
        "🔨 Member Banned",
        f"**User:** {user}\n"
        f"**ID:** `{user.id}`",
        discord.Color.red()
    )


@bot.event
async def on_member_unban(guild, user):
    await send_log(
        guild,
        "🔓 Member Unbanned",
        f"**User:** {user}\n"
        f"**ID:** `{user.id}`",
        discord.Color.green()
    )


@bot.event
async def on_message_delete(message):
    if message.guild is None:
        return

    author = message.author

    await send_log(
        message.guild,
        "🗑️ Message Deleted",
        f"**Author:** "
        f"{author.mention if author else 'Unknown'}\n"
        f"**Channel:** {message.channel.mention}\n"
        f"**Content:** {clean_text(message.content)}",
        discord.Color.red()
    )


@bot.event
async def on_bulk_message_delete(messages):
    if not messages:
        return

    guild = messages[0].guild

    if guild is None:
        return

    await send_log(
        guild,
        "🗑️ Messages Bulk Deleted",
        f"**Channel:** {messages[0].channel.mention}\n"
        f"**Amount:** {len(messages)}",
        discord.Color.red()
    )


@bot.event
async def on_message_edit(before, after):
    if before.guild is None:
        return

    if before.content == after.content:
        return

    await send_log(
        before.guild,
        "✏️ Message Edited",
        f"**Author:** {before.author.mention}\n"
        f"**Channel:** {before.channel.mention}\n"
        f"**Before:** {clean_text(before.content)}\n"
        f"**After:** {clean_text(after.content)}",
        discord.Color.orange()
    )


@bot.event
async def on_guild_channel_create(channel):
    await send_log(
        channel.guild,
        "📁 Channel Created",
        f"**Channel:** "
        f"{channel.mention if hasattr(channel, 'mention') else channel.name}\n"
        f"**Type:** {channel.type}",
        discord.Color.green()
    )


@bot.event
async def on_guild_channel_delete(channel):
    await send_log(
        channel.guild,
        "🗑️ Channel Deleted",
        f"**Channel:** `{channel.name}`\n"
        f"**Type:** {channel.type}",
        discord.Color.red()
    )


@bot.event
async def on_guild_role_create(role):
    await send_log(
        role.guild,
        "🏷️ Role Created",
        f"**Role:** {role.mention}",
        discord.Color.green()
    )


@bot.event
async def on_guild_role_delete(role):
    await send_log(
        role.guild,
        "🗑️ Role Deleted",
        f"**Role:** `{role.name}`",
        discord.Color.red()
    )


@bot.event
async def on_member_update(before, after):
    before_roles = {
        role.id
        for role in before.roles
    }

    after_roles = {
        role.id
        for role in after.roles
    }

    if before_roles == after_roles:
        return

    added = after_roles - before_roles
    removed = before_roles - after_roles

    changes = []

    for role_id in added:
        role = after.guild.get_role(role_id)

        if role:
            changes.append(
                f"➕ {role.mention}"
            )

    for role_id in removed:
        role = after.guild.get_role(role_id)

        if role:
            changes.append(
                f"➖ {role.name}"
            )

    if changes:
        await send_log(
            after.guild,
            "🏷️ Member Roles Updated",
            f"**Member:** {after.mention}\n"
            + "\n".join(changes),
            discord.Color.blue()
        )


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild:
        key = (
            message.guild.id,
            message.author.id
        )

        now = time.time()

        spam_tracker[key].append(
            now
        )

        while (
            spam_tracker[key]
            and now - spam_tracker[key][0]
            > AUTO_SPAM_WINDOW
        ):
            spam_tracker[key].popleft()

        if len(spam_tracker[key]) >= AUTO_SPAM_LIMIT:
            if (
                not message.author.guild_permissions.administrator
                and message.guild.me
                and message.author.top_role
                < message.guild.me.top_role
            ):
                try:
                    await message.author.timeout(
                        timedelta(
                            seconds=AUTO_SPAM_TIMEOUT
                        ),
                        reason="Automatic anti-spam timeout"
                    )

                    await send_log(
                        message.guild,
                        "🚨 Automatic Anti-Spam",
                        f"**Member:** {message.author.mention}\n"
                        f"**Duration:** {AUTO_SPAM_TIMEOUT} seconds",
                        discord.Color.red()
                    )

                    spam_tracker[key].clear()

                except discord.HTTPException:
                    pass

    await bot.process_commands(
        message
    )


@bot.event
async def on_app_command_error(
    interaction,
    error
):
    if isinstance(
        error,
        app_commands.CheckFailure
    ):
        return

    if isinstance(
        error,
        app_commands.MissingPermissions
    ):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.",
                ephemeral=True
            )

        return

    if isinstance(
        error,
        app_commands.TransformerError
    ):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ One of the arguments is invalid.",
                ephemeral=True
            )

        return

    print(
        f"App command error: {error}"
    )

    if not interaction.response.is_done():
        await interaction.response.send_message(
            "❌ An error occurred while running this command.",
            ephemeral=True
        )


async def main():
    await bot.start(
        TOKEN
    )


asyncio.run(main())