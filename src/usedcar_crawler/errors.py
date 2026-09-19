"""异常体系：按"跳过 / 重试 / 告警"三种处置方式分级。

设计约定：
- 继承自 ``CrawlerError`` 的异常在单条数据粒度被捕获，不会中断整批任务；
- ``FatalError`` 用于配置错误等必须立即终止的场景；
- ``retryable`` 标记决定 tenacity 是否重试。
"""

from __future__ import annotations


class CrawlerError(Exception):
    """所有业务异常的基类。"""

    retryable: bool = False

    def __init__(self, message: str, *, context: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context = context or {}

    def __str__(self) -> str:  # pragma: no cover - 纯展示
        if not self.context:
            return self.message
        detail = " ".join(f"{k}={v}" for k, v in self.context.items())
        return f"{self.message} | {detail}"


class ConfigError(CrawlerError):
    """配置缺失或非法（如选择器未定义、数据库 URL 错误）。"""


class FatalError(CrawlerError):
    """不可恢复，立即终止进程。"""


class RobotsDeniedError(CrawlerError):
    """robots.txt 明确禁止抓取目标路径 —— 一票否决，绝不重试。"""


class FetchError(CrawlerError):
    """网络请求失败（超时、5xx、连接重置）。"""

    retryable = True


class NotFoundError(FetchError):
    """404 / 410：页面不存在。

    注意：Scrapling 的 Fetcher 对 404 **不会抛异常**，而是正常返回 Response。
    若不显式判定状态码，就会把 404 页面当成功数据入库——所以这里必须有闸门。
    重试与升档都无意义，直接失败。
    """

    retryable = False


class BlockedError(FetchError):
    """被反爬拦截（403 / 429 / 验证码页）。

    处置策略：**不原地重试，直接升档**。同一个出口被拦，重试 3 次只是浪费额度并加重风控；
    换更强的档位（HTTP -> 隐身 -> 浏览器）才有意义，这也是档位阶梯存在的理由。
    """

    retryable = False


class ParseError(CrawlerError):
    """页面结构无法解析（容器选择器零命中）。"""


class SchemaDriftError(ParseError):
    """必需字段缺失率超阈值，判定为站点改版，触发告警。"""


class NormalizeError(CrawlerError):
    """字段标准化失败（如价格文案无法提取数值）。"""


class StorageError(CrawlerError):
    """数据库读写失败。"""

    retryable = True
