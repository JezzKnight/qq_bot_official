"""查询/攻略类问题的识别：命中时给系统提示词追加一段信息型规则。

只做意图判断，不调模型；宁可漏判（退回闲聊风格）也不要误判
（把普通接话变成教程）。
"""

from __future__ import annotations

import re

_QUERY_PATTERN = re.compile(
    r"(怎么打|怎么过|怎么配|怎么组队|怎么获得|怎么解锁|怎么刷|"
    r"攻略|打法|配队|阵容|教程|"
    r"在哪(里|儿)?(买|刷|获得|打|换)?|多少钱|"
    r"有什么区别|有什么区别|推荐(一下|个|什么|哪)|"
    r"参数|面板|机制|技能表|掉落)",
    re.IGNORECASE,
)


def is_query_question(text: str) -> bool:
    """判断一条消息是不是查询/攻略类问题。"""
    return bool(_QUERY_PATTERN.search(text or ""))
