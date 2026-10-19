import os

from dotenv import dotenv_values

import nonebot

# PLATFORM 必须在 nonebot.init() 之前确定，而 .env.qq 是 NoneBot 在 init 时才读的。
# 这里按 NoneBot 的规则手动加载一次（.env → .env.{ENVIRONMENT}），否则永远取到默认 onebot。
_ENV = (os.getenv("ENVIRONMENT") or "dev").strip()
_env_values: dict[str, str] = {}
_env_values.update(dotenv_values(".env"))
_env_values.update(dotenv_values(f".env.{_ENV}"))
# 进程环境优先于 .env 文件，便于临时 PORT=xxxx 覆盖
for _key, _value in _env_values.items():
    if _value is not None:
        os.environ.setdefault(_key, _value)
PLATFORM = os.getenv("PLATFORM", "onebot").strip().lower()

nonebot.init()
driver = nonebot.get_driver()

if PLATFORM == "qq":
    # QQ 开放平台（官方 API，WebSocket 出站）
    from nonebot.adapters.qq import Adapter as QQAdapter

    driver.register_adapter(QQAdapter)
    nonebot.load_plugin("qq_bot.plugins.qqbot_official")
else:
    # OneBot V11（NapCat / go-cqhttp 反向上报）
    from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

    driver.register_adapter(OneBotV11Adapter)
    nonebot.load_plugin("qq_bot.plugins.scheduled_tasks")
    nonebot.load_plugin("qq_bot.plugins.ai_chat")

nonebot.load_builtin_plugins("echo", "single_session")

if __name__ == "__main__":
    nonebot.run()
