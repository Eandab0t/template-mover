"""Offline tests for TemplateMover: permission copying and dry-run behavior.

No network, no bot token. Run:  python tests/test_templatemover.py
"""

import asyncio
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord  # noqa: E402
import templatemover as tm  # noqa: E402


class FakeObj(types.SimpleNamespace):
    """SimpleNamespace with restored hashability (used as dict keys)."""
    __hash__ = object.__hash__


def fake_role(rid, name, position=0, managed=False, permissions=0,
              colour=None, hoist=False, mentionable=False):
    return FakeObj(
        id=rid, name=name, position=position,
        is_managed=lambda: managed, is_default=lambda: False,
        permissions=discord.Permissions(permissions),
        colour=discord.Colour(colour if colour is not None else 0),
        hoist=hoist, mentionable=mentionable)


def fake_everyone(rid=0):
    return FakeObj(
        id=rid, name="@everyone", position=0,
        is_managed=lambda: False, is_default=lambda: True,
        permissions=discord.Permissions(0), colour=discord.Colour(0),
        hoist=False, mentionable=False)


def fake_channel(rid, name, ctype, overwrites=None, category=None, **attrs):
    base = dict(topic=None, slowmode_delay=0, nsfw=False,
                user_limit=0, bitrate=64000)
    base.update(attrs)
    return FakeObj(id=rid, name=name, type=ctype,
                   overwrites=overwrites or {}, category=category, **base)


def fake_category(rid, name, channels, overwrites=None):
    return FakeObj(id=rid, name=name,
                   type=discord.ChannelType.category,
                   channels=channels, overwrites=overwrites or {},
                   category=None)


def fake_guild(rid, name, roles, categories, channels, default_role):
    return FakeObj(id=rid, name=name, roles=roles,
                   categories=categories, channels=channels,
                   default_role=default_role)


class OverwritePairTests(unittest.TestCase):
    def test_allow_deny_split(self):
        po = discord.PermissionOverwrite(manage_messages=True, read_messages=False)
        allow, deny = tm.overwrites_to_pairs(po)
        self.assertEqual(allow, ["manage_messages"])
        self.assertEqual(deny, ["read_messages"])

    def test_unset_flags_are_absent(self):
        po = discord.PermissionOverwrite(send_messages=True)
        allow, deny = tm.overwrites_to_pairs(po)
        self.assertNotIn("view_channel", allow + deny)
        self.assertEqual(deny, [])


class MapOverwritesTests(unittest.TestCase):
    def test_mapped_role_survives_with_target_name(self):
        role_map = {1: "Mods", 0: "@everyone"}
        overwrites = {
            fake_role(1, "Mods"): discord.PermissionOverwrite(manage_messages=True),
            fake_everyone(): discord.PermissionOverwrite(view_channel=False),
        }
        entries, notes = tm.map_overwrites(overwrites, role_map)
        self.assertEqual(entries, [
            {"role": "Mods", "allow": ["manage_messages"], "deny": []},
            {"role": "@everyone", "allow": [], "deny": ["read_messages"]},
        ])
        self.assertEqual(notes, [])

    def test_unmapped_role_is_dropped_with_note(self):
        entries, notes = tm.map_overwrites(
            {fake_role(7, "Ghost"): discord.PermissionOverwrite(manage_messages=True)},
            {})
        self.assertEqual(entries, [])
        self.assertIn("Ghost", notes[0])

    def test_member_overwrite_is_dropped_with_note(self):
        member = FakeObj(id=99, name="alice")
        entries, notes = tm.map_overwrites(
            {member: discord.PermissionOverwrite(send_messages=True)}, {})
        self.assertEqual(entries, [])
        self.assertIn("alice", notes[0])

    def test_all_unset_overwrite_produces_no_entry(self):
        entries, notes = tm.map_overwrites(
            {fake_role(1, "Mods"): discord.PermissionOverwrite()}, {1: "Mods"})
        self.assertEqual(entries, [])


def sample_template():
    mod_perms = discord.Permissions(0)
    mod_perms.update(manage_messages=True, kick_members=True)
    mods = fake_role(10, "Mods", position=2, colour=0x3498db, hoist=True,
                     permissions=mod_perms.value)
    everyone = fake_everyone()
    general = fake_channel(20, "general", discord.ChannelType.text,
                           overwrites={mods: discord.PermissionOverwrite(manage_messages=True),
                                       FakeObj(id=99, name="alice"):
                                           discord.PermissionOverwrite(send_messages=True)},
                           topic="hello", slowmode_delay=5)
    voice = fake_channel(21, "Stage room", discord.ChannelType.voice, user_limit=7)
    forum = fake_channel(22, "ideas", discord.ChannelType.forum)
    lounge = fake_category(30, "Lounge", [general, voice, forum],
                           overwrites={mods: discord.PermissionOverwrite(read_messages=True)})
    for channel in (general, voice, forum):
        channel.category = lounge
    welcome = fake_channel(23, "welcome", discord.ChannelType.text)
    return fake_guild(1, "Template Guild", [everyone, mods], [lounge],
                      [lounge, general, voice, forum, welcome], everyone)


class PlanGuildTests(unittest.TestCase):
    def setUp(self):
        self.target = fake_guild(2, "Target Guild", [fake_everyone(100)], [], [],
                                 fake_everyone(100))
        self.template = sample_template()
        self.plan, self.notes = tm.plan_guild(self.template, self.target)

    def test_plan_order_roles_categories_channels(self):
        actions = [item["action"] for item in self.plan]
        self.assertEqual(actions, ["role", "category", "channel", "channel", "channel"])

    def test_role_action_carries_attributes(self):
        expected = discord.Permissions(0)
        expected.update(manage_messages=True, kick_members=True)
        role = self.plan[0]
        self.assertEqual(role["name"], "Mods")
        self.assertEqual(role["color"], "#3498db")
        self.assertTrue(role["hoist"])
        self.assertEqual(role["permissions"], expected.value)

    def test_channel_kinds_and_attributes(self):
        channels = [i for i in self.plan if i["action"] == "channel"]
        self.assertEqual(channels[0]["kind"], "text")
        self.assertEqual(channels[0]["category"], "Lounge")
        self.assertEqual(channels[0]["topic"], "hello")
        self.assertEqual(channels[0]["slowmode"], 5)
        self.assertEqual(channels[1]["kind"], "voice")
        self.assertEqual(channels[1]["user_limit"], 7)
        self.assertEqual(channels[2]["category"], None)

    def test_overwrites_mapped_to_future_role_name(self):
        channels = [i for i in self.plan if i["action"] == "channel"]
        entry = channels[0]["overwrites"][0]
        self.assertEqual(entry["role"], "Mods")
        self.assertEqual(entry["allow"], ["manage_messages"])

    def test_unsupported_and_member_skips_noted(self):
        text = "\n".join(self.notes)
        self.assertIn("forum channel 'ideas'", text)
        self.assertIn("alice", text)

    def test_dry_run_changes_nothing(self):
        # The fakes expose no create_* methods at all: any mutation attempt
        # would raise. Template guild must come out identical.
        self.assertEqual(len(self.template.roles), 2)
        self.assertEqual(len(self.template.categories), 1)
        self.assertEqual(self.plan[0]["action"], "role")

    def test_print_plan_shows_dry_run_banner(self):
        out = io.StringIO()
        with redirect_stdout(out):
            tm.print_plan(self.plan, self.notes, self.template, self.target, apply_mode=False)
        text = out.getvalue()
        self.assertIn("dry run (nothing will be created)", text)
        self.assertIn("[+] create role Mods", text)
        self.assertIn("[+] create category Lounge", text)
        self.assertIn("[+] create text #general", text)
        self.assertIn("overwrites: Mods (+manage_messages)", text)
        self.assertIn("[!] skip forum channel 'ideas'", text)
        self.assertNotIn("[x]", text)  # nothing failed in a dry run


class PlanReuseTests(unittest.TestCase):
    def test_existing_role_category_and_channel_are_reused(self):
        target_cat = fake_category(300, "Lounge", [])
        target_general = fake_channel(310, "general", discord.ChannelType.text,
                                      category=target_cat)
        target = fake_guild(2, "Target Guild",
                            [fake_everyone(100), fake_role(110, "Mods")],
                            [target_cat], [target_cat, target_general],
                            fake_everyone(100))
        plan, notes = tm.plan_guild(sample_template(), target)
        # Roles and the category map onto existing objects; only channels the
        # target lacks are still planned.
        self.assertEqual([i["action"] for i in plan if i["action"] != "channel"], [])
        self.assertEqual([i["name"] for i in plan], ["Stage room", "welcome"])
        text = "\n".join(notes)
        self.assertIn("reusing existing target role", text)
        self.assertIn("category 'Lounge' already exists", text)
        self.assertIn("channel #general already exists", text)


class ConfirmTests(unittest.TestCase):
    def test_exact_apply_confirms(self):
        self.assertTrue(tm.confirmed(3, "T", input_fn=lambda _: "APPLY"))

    def test_anything_else_cancels(self):
        self.assertFalse(tm.confirmed(3, "T", input_fn=lambda _: "yes"))

    def test_eof_cancels(self):
        def raise_eof(_):
            raise EOFError
        self.assertFalse(tm.confirmed(3, "T", input_fn=raise_eof))


class ApplyPlanTests(unittest.IsolatedAsyncioTestCase):
    def make_target(self):
        everyone = fake_everyone(100)
        target = FakeObj(id=2, name="Target Guild",
                         roles=[everyone], default_role=everyone,
                         categories=[])
        created_role = FakeObj(id=500, name="Mods")

        async def create_role(**kwargs):
            target.roles.append(created_role)
            role_calls.append(kwargs)
            return created_role

        async def create_category(**kwargs):
            cat_calls.append(kwargs)
            cat = FakeObj(id=600, name=kwargs["name"])
            target.categories.append(cat)  # the real library updates its cache too
            return cat

        async def create_text_channel(**kwargs):
            ch_calls.append(kwargs)
            return types.SimpleNamespace(id=700, name=kwargs["name"])

        target.create_role = create_role
        target.create_category = create_category
        target.create_text_channel = create_text_channel
        role_calls, cat_calls, ch_calls = [], [], []
        return target, role_calls, cat_calls, ch_calls

    async def test_apply_creates_roles_categories_channels(self):
        target, role_calls, cat_calls, ch_calls = self.make_target()
        plan = [
            {"action": "role", "name": "Mods", "color": "#3498db", "hoist": True,
             "mentionable": False, "permissions": 64, "permission_names": ["manage_messages"]},
            {"action": "category", "name": "Lounge",
             "overwrites": [{"role": "Mods", "allow": ["view_channel"], "deny": []}]},
            {"action": "channel", "kind": "text", "name": "general", "category": "Lounge",
             "topic": "hi", "slowmode": 3, "nsfw": False, "news": False,
             "overwrites": []},
        ]
        with patch("asyncio.sleep", new_callable=AsyncMock):
            created, errors = await tm.apply_plan(plan, target)
        self.assertEqual(errors, 0)
        self.assertEqual(created["roles"][0]["id"], 500)
        self.assertEqual(created["categories"][0]["name"], "Lounge")
        self.assertEqual(created["channels"][0]["category_id"], 600)
        self.assertEqual(role_calls[0]["permissions"], discord.Permissions(64))
        self.assertEqual(role_calls[0]["colour"], discord.Colour(0x3498db))
        # category overwrite resolved to the created role object
        created_role_key = next(iter(cat_calls[0]["overwrites"]))
        self.assertIn(created_role_key, target.roles)
        self.assertTrue(cat_calls[0]["overwrites"][created_role_key].read_messages is True)
        self.assertEqual(ch_calls[0]["category"].id, 600)
        self.assertEqual(ch_calls[0]["topic"], "hi")

    async def test_one_failure_does_not_stop_the_rest(self):
        target, role_calls, cat_calls, ch_calls = self.make_target()

        async def boom(**kwargs):
            raise discord.HTTPException(None, "rate limited")

        target.create_category = boom
        plan = [
            {"action": "role", "name": "Mods", "color": None, "hoist": False,
             "mentionable": False, "permissions": 0, "permission_names": []},
            {"action": "category", "name": "Broken", "overwrites": []},
            {"action": "channel", "kind": "text", "name": "general", "category": None,
             "topic": "", "slowmode": 0, "nsfw": False, "news": False, "overwrites": []},
        ]
        err = io.StringIO()
        with patch("asyncio.sleep", new_callable=AsyncMock), redirect_stderr(err):
            created, errors = await tm.apply_plan(plan, target)
        self.assertEqual(errors, 1)
        self.assertEqual(len(created["roles"]), 1)
        self.assertEqual(len(created["channels"]), 1)
        self.assertIn("Broken", err.getvalue())


class ExportTests(unittest.TestCase):
    def test_export_lists_what_was_created(self):
        template = fake_guild(1, "T", [], [], [], fake_everyone())
        target = fake_guild(2, "G", [], [], [], fake_everyone())
        created = {"roles": [{"name": "Mods", "id": 500}],
                   "categories": [], "channels": [{"kind": "text", "name": "general",
                                                   "id": 700, "category_id": None}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = tm.write_export(created, ["skipped thing"], template, target,
                                   directory=tmp)
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(data["created"]["roles"], [{"name": "Mods", "id": 500}])
        self.assertEqual(data["skipped"], ["skipped thing"])
        self.assertEqual(data["target_guild"]["id"], 2)
        self.assertTrue(Path(path).name.startswith("template-export-"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
