"""Offline behavioral checks: python -m unittest -v"""
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord
from bot import ApplyView, ReviewView, Store, make_bot, nickname_for, PersonalInfoModal, OptionalInfoView, GuildBot, ApplicationModal


class ValidationTests(unittest.TestCase):
    def test_nickname_validation(self):
        self.assertEqual(nickname_for(" 정우전사 ", "나이트로드", "087"), "정우전사 | 나이트로드 | Lv.87")
        for name, job, level in (("", "전사", "1"), ("a\nb", "전사", "1"),
                                 ("정우", "전사", "0"), ("정우", "전사", "201"),
                                 ("정우", "전사", "１２"), ("정우", "전사", "abc"),
                                 ("가" * 20, "나" * 20, "100")):
            with self.subTest(name=name, job=job, level=level), self.assertRaises(ValueError):
                nickname_for(name, job, level)

    def test_pending_unique_and_resubmission(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            sql = "INSERT INTO applications(user_id,name,job,level,note) VALUES (1,'x','y',1,'')"
            store.run(sql)
            with self.assertRaises(sqlite3.IntegrityError):
                store.run(sql)
            store.run("UPDATE applications SET status='rejected'")
            store.run(sql)
            self.assertEqual(store.run("SELECT COUNT(*) FROM applications").fetchone()[0], 2)
            store.db.close()

    def test_old_database_migration_preserves_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, job TEXT, level INTEGER, note TEXT, status TEXT, message_id INTEGER, reviewer_id INTEGER, created_at TEXT, decided_at TEXT)")
            db.execute("INSERT INTO applications(id,user_id,name,status) VALUES (1,5,'기존신청','pending')")
            db.commit()
            db.close()
            for _ in range(2):
                store = Store(path)
                row = store.run("SELECT * FROM applications WHERE id=1").fetchone()
                self.assertEqual(row["name"], "기존신청")
                self.assertEqual((row["age"],row["gender"],row["region"]), ("","",""))
                self.assertEqual(row["referral"], "")
                store.db.close()


class BehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.bot = make_bot({"guild_id":"1", "review_channel_id":"2", "guild_role":"길드원",
                             "mercenary_role":"용병", "waiting_role":"가입대기", "admin_role":"관리자",
                             "welcome_message":"환영합니다", "log_channel_id":""}, self.store)
        self.bot.role = lambda k: {"guild_role":1, "mercenary_role":2, "waiting_role":3}[k]
        self.bot.is_admin = lambda i: True
        self.bot.audit = AsyncMock()
        self.bot.dm = AsyncMock(return_value=False)
        self.bot.ensure_review_private = lambda channel: None
        self.member = SimpleNamespace(id=5, roles=[2,3], top_role=4,
            edit=AsyncMock(), remove_roles=AsyncMock(), add_roles=AsyncMock(), bot=False)
        self.guild = SimpleNamespace(id=1, owner_id=999, me=SimpleNamespace(top_role=10),
                                     fetch_member=AsyncMock(return_value=self.member))
        self.member.guild = self.guild
        self.interaction = SimpleNamespace(guild=self.guild, guild_id=1, channel_id=2,
            user=SimpleNamespace(id=9), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), message=SimpleNamespace(id=33,
            embeds=[discord.Embed(title="신청")], edit=AsyncMock()))
        self.interaction.channel = SimpleNamespace()
        self.store.run("INSERT INTO applications(user_id,name,job,level,note,message_id) VALUES(5,'정우','전사',87,'',33)")

    async def asyncTearDown(self):
        await self.bot.close()
        self.store.db.close()
        self.tmp.cleanup()

    def status(self):
        return self.store.run("SELECT status FROM applications").fetchone()[0]

    async def test_approval_dm_failure_and_repeat(self):
        await self.bot.decide(self.interaction, True)
        self.assertEqual(self.status(), "approved")
        self.member.remove_roles.assert_awaited_once_with(2, 3, reason="길드원 승인")
        self.member.add_roles.assert_awaited_once_with(1, reason="길드원 승인")
        self.member.edit.assert_awaited_once()
        await self.bot.decide(self.interaction, True)
        self.assertEqual(self.bot.dm.await_count, 1)
        self.assertEqual(self.member.add_roles.await_count, 1)

    async def test_failed_approval_remains_retryable(self):
        self.member.add_roles.side_effect = discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "Denied")
        await self.bot.decide(self.interaction, True)
        self.assertEqual(self.status(), "pending")
        self.bot.dm.assert_not_awaited()
        self.member.add_roles.side_effect = None
        await self.bot.decide(self.interaction, True)
        self.assertEqual(self.status(), "approved")

    async def test_unauthorized_cannot_approve(self):
        self.bot.is_admin = lambda i: False
        await self.bot.decide(self.interaction, True)
        self.assertEqual(self.status(), "pending")
        self.member.edit.assert_not_awaited()

    async def test_rejection_preserves_roles(self):
        await self.bot.decide(self.interaction, False)
        self.assertEqual(self.status(), "rejected")
        self.member.edit.assert_not_awaited()
        self.member.remove_roles.assert_not_awaited()
        self.member.add_roles.assert_not_awaited()

    async def test_offline_role_conflict_repair(self):
        self.member.roles = [1,2,3,8]
        await self.bot.safe_normalize(self.member)
        self.member.remove_roles.assert_awaited_once_with(2,3,reason="가입 역할 중복 해소: 길드원 우선")
        self.member.add_roles.assert_not_awaited()

    async def test_views_survive_restart_registration(self):
        self.assertTrue(ApplyView().is_persistent())
        self.assertTrue(ReviewView().is_persistent())

    async def test_personal_fields_all_optional_and_separate(self):
        modal = PersonalInfoModal(OptionalInfoView(5, {}))
        self.assertEqual(len(modal.children), 3)
        self.assertTrue(all(not label.component.required for label in modal.children))
        self.assertEqual([label.text for label in modal.children], ["나이 (선택)","성별 (선택)","거주지역 (선택)"])
        self.assertTrue(all("관리자만 확인" in label.description for label in modal.children))

    async def test_blank_and_partial_personal_data_submission(self):
        self.store.run("DELETE FROM applications")
        self.interaction.user.id = 5
        self.bot.review_channel = lambda: SimpleNamespace()
        self.bot.post_review = AsyncMock()
        data = {"name":"정우", "job":"전사", "level":87, "note":"", "referral":"친구 소개 / 정우전사"}
        for personal in ({}, {"age":"20대", "region":"경기"}, {"age":"29", "gender":"남성", "region":"서울"}):
            draft = SimpleNamespace(user_id=5, submitted=False)
            ok = await self.bot.submit_application(self.interaction, data, personal, draft)
            self.assertTrue(ok)
            row = self.store.run("SELECT * FROM applications ORDER BY id DESC").fetchone()
            for key in ("age","gender","region"):
                self.assertEqual(row[key], personal.get(key,""))
            self.assertEqual(row["referral"], "친구 소개 / 정우전사")
            self.assertEqual(nickname_for(row["name"],row["job"],row["level"]), "정우 | 전사 | Lv.87")
            self.store.run("UPDATE applications SET status='rejected'")

    async def test_review_embed_keeps_personal_data_private(self):
        self.store.run("UPDATE applications SET age='29',gender='남성',region='경기' WHERE user_id=5")
        row = self.store.run("SELECT * FROM applications").fetchone()
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=44)))
        self.bot.review_channel = lambda: channel
        await self.bot.post_review(row)
        fields = channel.send.call_args.kwargs["embed"].fields
        self.assertEqual([f.value for f in fields[-3:]], ["29","남성","경기"])
        self.assertTrue(all("비공개" in f.name for f in fields[-3:]))
        self.bot.audit.assert_not_awaited()

    async def test_public_review_channel_blocks_submission(self):
        self.store.run("DELETE FROM applications")
        self.interaction.user.id = 5
        self.bot.review_channel = lambda: SimpleNamespace()
        self.bot.post_review = AsyncMock()
        def blocked(channel):
            raise ValueError("개인정보 보호: 관리자만 볼 수 있어야 합니다.")
        self.bot.ensure_review_private = blocked
        result = await self.bot.submit_application(self.interaction,
            {"name":"정우","job":"전사","level":87,"note":"", "referral":"검색 / 없음"}, {"age":"29"}, SimpleNamespace(user_id=5,submitted=False))
        self.assertFalse(result)
        self.assertEqual(self.store.run("SELECT COUNT(*) FROM applications").fetchone()[0], 0)
        self.bot.post_review.assert_not_awaited()

    async def test_referral_required_and_basic_form_fits(self):
        modal = ApplicationModal()
        self.assertEqual(len(modal.children), 5)
        self.assertTrue(modal.referral.required)
        self.store.run("DELETE FROM applications")
        self.interaction.user.id = 5
        self.bot.post_review = AsyncMock()
        for value in ("", "   ", "x" * 401):
            ok = await self.bot.submit_application(self.interaction,
                {"name":"정우","job":"전사","level":87,"note":"", "referral":value},
                {}, SimpleNamespace(user_id=5,submitted=False))
            self.assertFalse(ok)
        self.assertEqual(self.store.run("SELECT COUNT(*) FROM applications").fetchone()[0], 0)
        self.bot.post_review.assert_not_awaited()

    async def test_privacy_visibility_checks(self):
        everyone = SimpleNamespace(id=0,name="@everyone", permissions=SimpleNamespace(administrator=False),
            is_default=lambda:True, is_bot_managed=lambda:False)
        normal = SimpleNamespace(id=1,name="길드원",permissions=SimpleNamespace(administrator=False),
            is_default=lambda:False,is_bot_managed=lambda:False)
        guild = SimpleNamespace(default_role=everyone,roles=[everyone,normal],members=[])
        visibility = {0:False,1:False}
        channel = SimpleNamespace(guild=guild,permissions_for=lambda item:SimpleNamespace(view_channel=visibility[item.id]))
        GuildBot.ensure_review_private(self.bot,channel)
        visibility[1] = True
        with self.assertRaises(ValueError):
            GuildBot.ensure_review_private(self.bot,channel)
        visibility[1] = False
        visibility[0] = True
        with self.assertRaises(ValueError):
            GuildBot.ensure_review_private(self.bot,channel)


if __name__ == "__main__":
    unittest.main()
