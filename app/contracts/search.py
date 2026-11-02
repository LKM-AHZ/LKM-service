"""检索索引文档的共享契约。

``IndexDoc`` 是「内容行 → 引擎文档」的产物形状，content 与 files 各自产出、search 只消费。
放在 contracts 是因为**产在业务域、用在 search**：任何一侧 import 对方都会形成跨模块边。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

IndexDoc = Mapping[str, Any]
