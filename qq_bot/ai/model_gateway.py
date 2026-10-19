"""模型网关：唯一的模型解析入口，负责传输连接池。

纯服务层，不依赖 NoneBot / pydantic——配置由插件层 client_factory 映射成
参数传入。
"""

from .openai_client import OpenaiCompatClient


class ModelGateway:
    """模型网关：按 (base_url, api_key) 复用 OpenAI 兼容传输连接池。"""

    def __init__(self) -> None:
        self._clients: dict[tuple[str, str, float, float], OpenaiCompatClient] = {}

    def get_client(
        self,
        base_url: str,
        api_key: str,
        default_model: str = "deepseek-chat",
        timeout: float = 180.0,
        connect_timeout: float = 10.0,
    ) -> OpenaiCompatClient:
        """取（复用）某端点/密钥/超时组合对应的 OpenAI 兼容客户端。

        default_model 只在调用方省略 ``model`` 参数时生效；连接池键包含超时参数——
        同一端点但不同超时要求（如本地 VL 模型与云端模型）不能共用同一连接池。
        """
        key = (base_url, api_key, timeout, connect_timeout)
        client = self._clients.get(key)
        if client is None:
            client = OpenaiCompatClient(
                base_url=base_url,
                api_key=api_key,
                default_model=default_model,
                timeout=timeout,
                connect_timeout=connect_timeout,
            )
            self._clients[key] = client
        return client

    async def close_all(self) -> None:
        """幂等关闭全部连接池并清空缓存（供生命周期 shutdown 调用）。"""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
