"""Free guild application bot. Python 3.10+. Run exactly one instance per DB."""
import asyncio
from collections import defaultdict
import json
import logging
from pathlib import Path
import sqlite3
import unicodedata

import discord
from discord import app_commands

BASE = Path(__file__).resolve().parent
log = logging.getLogger("guild_bot")
PRIVACY_NOTICE = (
    "나이·성별·거주지역은 모두 선택 항목입니다. 불편한 항목은 비워두거나 전체를 건너뛰어도 가입 신청이 가능합니다. "
    "작성한 개인정보는 길드 관리 목적으로 관리자만 확인하며, 일반 길드원에게 공개하지 않습니다. "
    "거주지역은 시·도 정도만 적어주세요. 상세 주소는 적지 마세요."
)


def nickname_for(name, job, level):
    name, job = name.strip(), job.strip()
    if not name or not job or any(unicodedata.category(c).startswith("C") for c in name + job):
        raise ValueError("닉네임과 직업에는 빈 값이나 줄바꿈을 사용할 수 없습니다.")
    level = str(level).strip()
    if not level.isascii() or not level.isdecimal() or not 1 <= int(level) <= 200:
        raise ValueError("레벨은 1~200 사이의 숫자로 입력해주세요.")
    nick = f"{name} | {job} | Lv.{int(level)}"
    if len(nick) > 32:
        raise ValueError("서버 별명이 32자를 넘습니다. 닉네임 또는 직업을 짧게 입력해주세요.")
    return nick


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                name TEXT NOT NULL, job TEXT NOT NULL, level INTEGER NOT NULL,
                note TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                message_id INTEGER UNIQUE, reviewer_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, decided_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending
                ON applications(user_id) WHERE status='pending';
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
        # Upgrade the existing DB without removing application history.
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(applications)")}
        for column in ("age", "gender", "region", "referral"):
            if column not in columns:
                self.run(f"ALTER TABLE applications ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

    def run(self, sql, args=()):
        with self.db:
            return self.db.execute(sql, args)

    def setting(self, key):
        row = self.run("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        self.run("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))


class SafeView(discord.ui.View):
    async def on_error(self, interaction, error, item):
        log.error("Button error", exc_info=(type(error), error, error.__traceback__))
        text = "처리 중 오류가 발생했습니다. 운영진에게 알려주세요. 승인 오류라면 권한 확인 후 재시도할 수 있습니다."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class ApplicationModal(discord.ui.Modal, title="길드 가입 신청 — 기본정보"):
    character = discord.ui.TextInput(label="메이플랜드 닉네임", max_length=20)
    job = discord.ui.TextInput(label="직업", max_length=20)
    level = discord.ui.TextInput(label="레벨", placeholder="예: 87", max_length=3)
    referral = discord.ui.TextInput(
        label="길드 가입 경로 / 초대한 사람 (필수)",
        placeholder="길드 가입하게 된 경로가 어떻게 되시나요? 길드 초대한 사람 누군지 적어주세용. 없으면 '없음'",
        style=discord.TextStyle.paragraph, required=True, max_length=400)

    async def on_submit(self, interaction):
        try:
            nickname_for(str(self.character), str(self.job), str(self.level))
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        if not str(self.referral).strip():
            await interaction.response.send_message("길드 가입 경로와 초대한 사람을 적어주세요. 초대한 사람이 없으면 '없음'이라고 적어주세요.", ephemeral=True)
            return
        data = {"name": str(self.character).strip(), "job": str(self.job).strip(),
                "level": int(str(self.level)), "note": "",
                "referral": str(self.referral).strip()}
        view = OptionalInfoView(interaction.user.id, data)
        await interaction.response.send_message(
            "**선택 답변 항목 — 나이 / 성별 / 거주지**\n" + PRIVACY_NOTICE +
            "\n\n아래 버튼으로 선택정보를 작성하거나 건너뛰고 신청서를 제출해주세요. "
            "이 안내는 본인에게만 보입니다. 10분 안에 완료해주세요.",
            view=view, ephemeral=True)

    async def on_error(self, interaction, error):
        log.error("Modal error", exc_info=(type(error), error, error.__traceback__))
        if interaction.response.is_done():
            await interaction.followup.send("신청 처리 오류입니다. 운영진에게 알려주세요.", ephemeral=True)
        else:
            await interaction.response.send_message("신청 처리 오류입니다. 운영진에게 알려주세요.", ephemeral=True)


class OptionalInfoView(SafeView):
    def __init__(self, user_id, data):
        super().__init__(timeout=600)
        self.user_id, self.data = user_id, data
        self.submitted = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("본인의 신청서만 제출할 수 있습니다.", ephemeral=True)
            return False
        if self.submitted:
            await interaction.response.send_message("이미 제출한 신청서입니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="선택 답변 항목 열기", style=discord.ButtonStyle.primary)
    async def optional(self, interaction, button):
        await interaction.response.send_modal(PersonalInfoModal(self))

    @discord.ui.button(label="건너뛰고 신청서 제출", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction, button):
        if await interaction.client.submit_application(interaction, self.data, {}, self):
            await self.finish(interaction)

    async def finish(self, interaction):
        self.submitted = True
        self.stop()
        # The DB guards duplicate submissions even if editing this ephemeral UI fails.
        if interaction.message:
            try:
                await interaction.message.edit(content="신청서를 제출했습니다. 운영진의 심사 결과를 기다려주세요.", view=None)
            except discord.HTTPException:
                pass


class PersonalInfoModal(ApplicationModal, title="선택 답변 항목"):
    # Build a fresh modal so the inherited basic fields are not included.
    def __init__(self, parent):
        discord.ui.Modal.__init__(self, title="선택 답변 항목")
        self.clear_items()
        self.parent = parent
        self.add_item(discord.ui.TextDisplay("**개인정보 안내**\n" + PRIVACY_NOTICE))
        self.age = discord.ui.TextInput(style=discord.TextStyle.short, required=False, max_length=16, placeholder="예: 29 또는 20대 / 비워두기 가능")
        self.gender = discord.ui.TextInput(style=discord.TextStyle.short, required=False, max_length=24, placeholder="원하는 표현으로 입력 / 비워두기 가능")
        self.region = discord.ui.TextInput(style=discord.TextStyle.short, required=False, max_length=40, placeholder="예: 서울, 경기 / 상세 주소 제외")
        for label, field in (("나이 (선택)", self.age), ("성별 (선택)", self.gender), ("거주지 (선택)", self.region)):
            self.add_item(discord.ui.Label(text=label, description="길드 관리용 개인정보입니다. 관리자만 확인하며 공개하지 않습니다.", component=field))

    async def on_submit(self, interaction):
        personal = {"age": str(self.age).strip(), "gender": str(self.gender).strip(),
                    "region": str(self.region).strip()}
        if await interaction.client.submit_application(interaction, self.parent.data, personal, self.parent):
            await self.parent.finish(interaction)


class ApplyView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📝 가입 신청하기", style=discord.ButtonStyle.primary, custom_id="guild:apply:v1")
    async def apply(self, interaction, button):
        if interaction.guild_id != interaction.client.guild_id:
            await interaction.response.send_message("설정된 서버에서만 신청할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplicationModal())


class ReviewView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ 승인", style=discord.ButtonStyle.success, custom_id="guild:approve:v1")
    async def approve(self, interaction, button):
        await interaction.client.decide(interaction, True)

    @discord.ui.button(label="❌ 거절", style=discord.ButtonStyle.danger, custom_id="guild:reject:v1")
    async def reject(self, interaction, button):
        await interaction.client.decide(interaction, False)


class GuildBot(discord.Client):
    def __init__(self, config, store):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config, self.store = config, store
        self.guild_id = int(config["guild_id"])
        self.tree = app_commands.CommandTree(self)
        self.locks = defaultdict(asyncio.Lock)
        self.reconcile_task = None

    async def setup_hook(self):
        self.add_view(ApplyView())
        self.add_view(ReviewView())
        self.tree.copy_global_to(guild=discord.Object(id=self.guild_id))
        await self.tree.sync(guild=discord.Object(id=self.guild_id))

    def role(self, key):
        guild = self.get_guild(self.guild_id)
        matches = [r for r in guild.roles if r.name == self.config[key]]
        if len(matches) != 1:
            raise ValueError(f"역할 {self.config[key]}: 역할이 없거나 같은 이름이 여러 개입니다.")
        return matches[0]

    def is_admin(self, interaction):
        if interaction.guild_id != self.guild_id or not isinstance(interaction.user, discord.Member):
            return False
        return (interaction.user.guild_permissions.administrator
                or any(r.name == self.config["admin_role"] for r in interaction.user.roles))

    def review_channel(self):
        ch = self.get_channel(int(self.config["review_channel_id"]))
        if not isinstance(ch, discord.TextChannel) or ch.guild.id != self.guild_id:
            raise ValueError("가입심사 채널 ID를 확인해주세요.")
        return ch

    def ensure_review_private(self, channel):
        guild = channel.guild
        # Check effective visibility, including member-specific permission overrides.
        if channel.permissions_for(guild.default_role).view_channel:
            raise ValueError("개인정보 보호: 가입심사 채널에서 @everyone의 채널 보기를 차단해주세요.")
        for role in guild.roles:
            if (role.is_default() or role.permissions.administrator
                    or role.name == self.config["admin_role"]
                    or (role.is_bot_managed() and role.tags.bot_id == self.user.id)):
                continue
            if channel.permissions_for(role).view_channel:
                raise ValueError(f"개인정보 보호: 가입심사 채널이 '{role.name}' 역할에 공개되어 있습니다. 관리자와 봇만 보도록 수정해주세요.")
        for member in guild.members:
            if member.id == self.user.id or member.guild_permissions.administrator or any(r.name == self.config["admin_role"] for r in member.roles):
                continue
            if channel.permissions_for(member).view_channel:
                raise ValueError("개인정보 보호: 가입심사 채널에 일반 멤버의 보기 권한이 있습니다. 관리자와 봇만 보도록 수정해주세요.")

    def validate(self):
        guild = self.get_guild(self.guild_id)
        if not guild:
            raise ValueError("서버 ID가 틀리거나 봇이 서버에 초대되지 않았습니다.")
        if not guild.me.guild_permissions.manage_roles or not guild.me.guild_permissions.manage_nicknames:
            raise ValueError("봇에 역할 관리와 별명 관리 권한을 주세요.")
        roles = [self.role(k) for k in ("guild_role", "mercenary_role", "waiting_role")]
        if len({r.id for r in roles}) != 3 or any(r.managed or r >= guild.me.top_role or r.is_default() for r in roles):
            raise ValueError("세 역할은 서로 달라야 하며 봇보다 아래에 있어야 합니다.")
        ch = self.review_channel()
        perms = ch.permissions_for(guild.me)
        if not all((perms.view_channel, perms.send_messages, perms.embed_links, perms.read_message_history)):
            raise ValueError("가입심사 채널에서 봇의 보기/메시지/링크 첨부/기록 읽기 권한을 허용해주세요.")
        self.ensure_review_private(ch)

    async def on_ready(self):
        try:
            self.validate()
        except ValueError as e:
            print(f"설정 오류: {e}")
            await self.close()
            return
        print(f"봇 온라인: {self.user}. Discord에서 /가입설치 명령을 사용하세요.")
        if self.reconcile_task is None or self.reconcile_task.done():
            self.reconcile_task = asyncio.create_task(self.reconcile())

    async def reconcile(self):
        # Offline role changes and unfinished review deliveries are repaired on reconnect.
        for member in list(self.get_guild(self.guild_id).members):
            await self.safe_normalize(member)
        rows = self.store.run("SELECT * FROM applications WHERE status='pending' AND message_id IS NULL").fetchall()
        for row in rows:
            try:
                await self.post_review(row)
            except (discord.HTTPException, ValueError):
                log.exception("Undelivered application %s", row["id"])

    async def submit_application(self, interaction, data, personal, draft):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild_id != self.guild_id or interaction.user.id != draft.user_id:
            await interaction.followup.send("본인의 서버 신청서만 제출할 수 있습니다.", ephemeral=True)
            return False
        referral = str(data.get("referral", "")).strip()
        if not referral or len(referral) > 400:
            await interaction.followup.send("길드 가입 경로 / 초대한 사람은 필수 항목입니다. 400자 이내로 작성해주세요. 초대한 사람이 없으면 '없음'이라고 적어주세요.", ephemeral=True)
            return False
        async with self.locks[interaction.user.id]:
            if draft.submitted:
                await interaction.followup.send("이미 제출한 신청서입니다.", ephemeral=True)
                return False
            member = await interaction.guild.fetch_member(interaction.user.id)
            if self.role("guild_role") in member.roles:
                await interaction.followup.send("이미 길드원입니다.", ephemeral=True)
                return False
            try:
                self.ensure_review_private(self.review_channel())
            except ValueError as e:
                await interaction.followup.send(str(e) + " 운영진에게 알려주세요. 신청서는 아직 제출되지 않았습니다.", ephemeral=True)
                return False
            try:
                cur = self.store.run(
                    "INSERT INTO applications(user_id,name,job,level,note,age,gender,region,referral) VALUES (?,?,?,?,?,?,?,?,?)",
                    (member.id, data["name"], data["job"], data["level"], data["note"],
                     personal.get("age", ""), personal.get("gender", ""), personal.get("region", ""), referral))
            except sqlite3.IntegrityError:
                await interaction.followup.send("이미 심사 중인 신청서가 있습니다.", ephemeral=True)
                return False
            app_id = cur.lastrowid
            row = self.store.run("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
            try:
                await self.post_review(row)
            except (discord.HTTPException, ValueError):
                self.store.run("DELETE FROM applications WHERE id=?", (app_id,))
                await interaction.followup.send("비공개 심사 채널 전송 실패입니다. 운영진에게 알려주세요. 다시 신청할 수 있습니다.", ephemeral=True)
                log.exception("Review delivery failed")
                return False
            draft.submitted = True
        await interaction.followup.send("신청서를 제출했습니다. 선택 개인정보는 관리자만 확인합니다.", ephemeral=True)
        return True

    async def post_review(self, row):
        channel = self.review_channel()
        self.ensure_review_private(channel)
        embed = discord.Embed(title="📋 길드 가입 신청", color=discord.Color.blue())
        for label, value in (("신청번호", row["id"]), ("신청자", f'<@{row["user_id"]}>'),
                             ("닉네임", row["name"]), ("직업", row["job"]), ("레벨", row["level"]),
                             ("가입 경로 / 초대한 사람", row["referral"] or "기존 신청서: 미수집"),
                             ("추가 내용", row["note"] or "없음")):
            embed.add_field(name=label, value=str(value), inline=False)
        for label, key in (("나이 (비공개·선택)", "age"), ("성별 (비공개·선택)", "gender"), ("거주지역 (비공개·선택)", "region")):
            embed.add_field(name=label, value=row[key] or "미작성", inline=False)
        embed.set_footer(text="선택 개인정보는 길드 관리 목적으로 관리자만 확인합니다. 일반 채널에 공유하지 마세요.")
        message = await channel.send(embed=embed, view=ReviewView())
        self.store.run("UPDATE applications SET message_id=? WHERE id=?", (message.id, row["id"]))

    async def dm(self, member, text):
        try:
            await member.send(text)
            return True
        except discord.HTTPException:
            return False

    async def audit(self, text):
        log.info(text)
        raw = self.config.get("log_channel_id")
        channel = self.get_channel(int(raw)) if raw else None
        if isinstance(channel, discord.TextChannel) and channel.guild.id == self.guild_id:
            try:
                await channel.send(text)
            except discord.HTTPException:
                log.warning("Audit channel delivery failed")

    async def decide(self, interaction, approve):
        if not self.is_admin(interaction) or interaction.channel_id != int(self.config["review_channel_id"]):
            await interaction.response.send_message("관리자만 가입심사 채널에서 처리할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            self.ensure_review_private(interaction.channel)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        row = self.store.run("SELECT * FROM applications WHERE message_id=?", (interaction.message.id,)).fetchone()
        if not row:
            await interaction.followup.send("저장된 신청서가 없습니다.", ephemeral=True)
            return
        async with self.locks[row["user_id"]]:
            row = self.store.run("SELECT * FROM applications WHERE id=?", (row["id"],)).fetchone()
            if row["status"] != "pending":
                await interaction.followup.send("이미 처리된 신청서입니다.", ephemeral=True)
                return
            try:
                member = await interaction.guild.fetch_member(row["user_id"])
            except discord.NotFound:
                await interaction.followup.send("신청자가 서버를 떠났습니다. 신청서는 보류합니다.", ephemeral=True)
                return
            if approve:
                nick = nickname_for(row["name"], row["job"], row["level"])
                if member.id == interaction.guild.owner_id or member.top_role >= interaction.guild.me.top_role:
                    await interaction.followup.send("서버장 또는 봇 이상 역할의 별명은 변경할 수 없습니다. 일반 테스트 계정으로 확인해주세요.", ephemeral=True)
                    return
                try:
                    await member.edit(nick=nick, reason=f"길드 가입 신청 {row['id']} 승인")
                    remove = [r for r in (self.role("mercenary_role"), self.role("waiting_role")) if r in member.roles]
                    if remove:
                        await member.remove_roles(*remove, reason="길드원 승인")
                    await member.add_roles(self.role("guild_role"), reason="길드원 승인")
                except discord.HTTPException:
                    log.exception("Approval incomplete for application %s", row["id"])
                    await interaction.followup.send("별명/역할 변경 중 실패했습니다. 일부 변경이 적용됐을 수 있습니다. 신청서는 심사 중으로 유지되며 권한 확인 후 승인을 재시도해주세요.", ephemeral=True)
                    return
            status = "approved" if approve else "rejected"
            self.store.run("UPDATE applications SET status=?,reviewer_id=?,decided_at=CURRENT_TIMESTAMP WHERE id=?",
                           (status, interaction.user.id, row["id"]))
            text = (f"🎉 길드 가입이 승인되었습니다!\n서버 별명: {nick}\n\n{self.config['welcome_message']}"
                    if approve else "길드 가입 신청이 거절되었습니다. 자세한 내용은 운영진에게 문의해주세요.")
            dm_ok = await self.dm(member, text)
            label = "✅ 승인 완료" if approve else "❌ 거절 완료"
            embed = interaction.message.embeds[0].copy()
            embed.color = discord.Color.green() if approve else discord.Color.red()
            embed.add_field(name="심사 결과", value=f"{label}\n처리자: <@{interaction.user.id}>\nDM: {'전송 성공' if dm_ok else '전송 실패'}", inline=False)
            try:
                await interaction.message.edit(embed=embed, view=None)
            except discord.HTTPException:
                log.warning("Review UI update failed; DB decision remains final")
            await self.audit(f"신청 #{row['id']} / 사용자 {member.id} / {label} / 승인자 {interaction.user.id} / DM {'성공' if dm_ok else '실패'}")
        await interaction.followup.send(f"{label}. DM {'전송 성공' if dm_ok else '전송 실패(가입 처리에는 영향 없음)'}.", ephemeral=True)

    async def safe_normalize(self, member):
        if member.guild.id != self.guild_id or member.bot:
            return
        async with self.locks[member.id]:
            try:
                member = await member.guild.fetch_member(member.id)
                guild, merc, waiting = (self.role(k) for k in ("guild_role", "mercenary_role", "waiting_role"))
                remove = []
                if guild in member.roles and merc in member.roles:
                    remove.append(merc)
                if (guild in member.roles or merc in member.roles) and waiting in member.roles:
                    remove.append(waiting)
                if remove:
                    await member.remove_roles(*remove, reason="가입 역할 중복 해소: 길드원 우선")
                if guild not in member.roles and merc not in member.roles and waiting not in member.roles:
                    await member.add_roles(waiting, reason="가입 대기")
            except (discord.HTTPException, ValueError):
                log.exception("Role normalization failed for %s", member.id)

    async def on_member_join(self, member):
        await self.safe_normalize(member)

    async def on_member_update(self, before, after):
        if before.roles != after.roles:
            await self.safe_normalize(after)


def make_bot(config, store):
    bot = GuildBot(config, store)

    @bot.tree.command(name="가입설치", description="선택한 채널에 길드 가입 신청 버튼 설치")
    @app_commands.guild_only()
    async def install(interaction: discord.Interaction, channel: discord.TextChannel):
        if not bot.is_admin(interaction):
            await interaction.response.send_message("관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot.validate()
        perms = channel.permissions_for(interaction.guild.me)
        if not all((perms.view_channel, perms.send_messages, perms.embed_links, perms.read_message_history)):
            await interaction.followup.send("선택한 채널에서 봇의 보기/메시지/링크 첨부/기록 읽기 권한을 허용해주세요.", ephemeral=True)
            return
        embed = discord.Embed(title="🍁 길드 가입 신청", description="길드 가입을 원하시면 아래 버튼을 눌러 닉네임·직업·레벨을 입력해주세요. 운영진 승인 후 역할과 서버 별명이 적용됩니다.\n\n**선택 개인정보 안내**\n" + PRIVACY_NOTICE, color=discord.Color.green())
        old_id = bot.store.setting(f"panel:{channel.id}")
        message = None
        if old_id:
            try:
                message = await channel.fetch_message(int(old_id))
                await message.edit(embed=embed, view=ApplyView())
            except discord.NotFound:
                message = None
        if message is None:
            message = await channel.send(embed=embed, view=ApplyView())
        bot.store.set(f"panel:{channel.id}", message.id)
        await interaction.followup.send("가입 신청 버튼을 설치했습니다.", ephemeral=True)

    @bot.tree.command(name="용병전환", description="관리자가 멤버를 용병으로 전환 (길드원 역할 제거)")
    @app_commands.guild_only()
    async def mercenary(interaction: discord.Interaction, member: discord.Member):
        if not bot.is_admin(interaction):
            await interaction.response.send_message("관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with bot.locks[member.id]:
            member = await interaction.guild.fetch_member(member.id)
            remove = [r for r in (bot.role("guild_role"), bot.role("waiting_role")) if r in member.roles]
            if remove:
                await member.remove_roles(*remove, reason="관리자 용병 전환")
            await member.add_roles(bot.role("mercenary_role"), reason="관리자 용병 전환")
        await bot.audit(f"관리자 {interaction.user.id}: 사용자 {member.id} 용병 전환")
        await interaction.followup.send("용병으로 전환했습니다.", ephemeral=True)

    @bot.tree.error
    async def command_error(interaction, error):
        log.error("Command error", exc_info=(type(error), error, error.__traceback__))
        text = "명령 처리 실패입니다. 설정과 봇 권한을 확인해주세요. 역할이 일부 변경됐다면 권한 수정 후 재시도해주세요."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    return bot


def main():
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(),
        logging.FileHandler(BASE / "bot.log", encoding="utf-8")])
    try:
        config = json.loads((BASE / "config.json").read_text(encoding="utf-8-sig"))
        for key in ("guild_id", "review_channel_id"):
            if not str(config[key]).isascii() or not str(config[key]).isdecimal():
                raise ValueError(f"{key}에 복사한 숫자 ID를 입력해주세요.")
        if config.get("log_channel_id") and not str(config["log_channel_id"]).isdecimal():
            raise ValueError("log_channel_id에는 숫자 ID 또는 빈 값을 입력해주세요.")
        if not config.get("token") or "여기에" in config["token"]:
            raise ValueError("config.json에 봇 토큰을 입력해주세요.")
        make_bot(config, Store(BASE / "applications.db")).run(config["token"])
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"설정 오류: {e}")
    except discord.LoginFailure:
        print("로그인 실패: Developer Portal에서 토큰을 재발급하고 config.json을 수정해주세요.")
    except discord.PrivilegedIntentsRequired:
        print("Developer Portal → Bot → SERVER MEMBERS INTENT를 켜주세요.")


if __name__ == "__main__":
    main()
