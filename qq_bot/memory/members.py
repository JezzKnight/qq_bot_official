"""群成员读模型：从仓储读取并构造 XML（平台无关）。"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from qq_bot.memory.naming import escape_for_prompt


async def members_list(repo: Any, group_id: int) -> list[dict] | None:
    """从 PG 读群成员；无成员返回 None。"""
    if repo is None:
        return None
    members = await repo.list_members(int(group_id))
    return members or None


async def members_xml(repo: Any, group_id: int) -> str | None:
    """构造群成员 XML（带 call 属性 = 该成员的称呼）。"""
    members = await members_list(repo, group_id)
    if not members:
        return None
    root = ET.Element("group_participants")
    for m in members:
        user = ET.SubElement(root, "user")
        user.set("id", str(m["user_id"]))
        name = m.get("card") or m.get("nickname") or str(m["user_id"])
        user.set("name", name)
        if m.get("call"):
            user.set("call", escape_for_prompt(str(m["call"])))
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", method="xml")
