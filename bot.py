from pathlib import Path
from typing import Any

import nonebot
from dotenv import load_dotenv
from nonebot.adapters.qq import Adapter as QQAdapter

from qq_bot.logging_config import init_logging

# NoneBot 只把 .env 读进 Config，不会写入 os.environ；PG_* 等由服务层用 os.getenv 读取
load_dotenv(Path(__file__).with_name(".env"))
nonebot.init()
init_logging()
GROUP_AUTHORIZE_STATUS = 20


class CompatQQAdapter(QQAdapter):
    """兼容新版 INTERACTION_CREATE type=20（群授权状态变更）。

    1.7.3 的事件模型只接受 type 11/12/13，遇到 type 20 会刷 ValidationError。
    type 20 不需要 ACK，直接忽略即可，不影响按钮回调 type=11。
    """

    def dispatch_event(self, bot: Any, payload: Any) -> None:
        data = payload.data if isinstance(payload.data, dict) else {}
        event_type = data.get("type")
        if (
            payload.type == "INTERACTION_CREATE"
            and event_type == GROUP_AUTHORIZE_STATUS
        ):
            return
        super().dispatch_event(bot, payload)


driver = nonebot.get_driver()
driver.register_adapter(CompatQQAdapter)
nonebot.load_plugin("qq_bot.plugins.qqbot_official")
nonebot.load_builtin_plugins("echo", "single_session")

if __name__ == "__main__":
    nonebot.run()
