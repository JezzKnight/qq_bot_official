from pathlib import Path

import nonebot
from dotenv import load_dotenv
from nonebot.adapters.qq import Adapter as QQAdapter

from qq_bot.logging_config import init_logging

# NoneBot 只把 .env 读进 Config，不会写入 os.environ；PG_* 等由服务层用 os.getenv 读取
load_dotenv(Path(__file__).with_name(".env"))
nonebot.init()
init_logging()
driver = nonebot.get_driver()
driver.register_adapter(QQAdapter)
nonebot.load_plugin("qq_bot.plugins.qqbot_official")
nonebot.load_builtin_plugins("echo", "single_session")

if __name__ == "__main__":
    nonebot.run()
