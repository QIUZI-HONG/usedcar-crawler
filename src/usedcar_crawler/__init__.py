"""二手车行情采集平台（EV / FUEL 双业务线）。

对外稳定 API：
    ``CrawlRunner`` / ``TaskSpec``  —— 采集任务编排
    ``FetcherService``             —— Scrapling 三档降级取数
    ``Repository``                 —— 存储读写
    ``export_records``             —— Excel / CSV 交付
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
