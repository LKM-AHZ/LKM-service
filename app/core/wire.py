"""读热序列化通用原语：msgspec 版 JSON 响应（roadmap §6.5.2，M5）。

分工：请求/响应**校验仍由 Pydantic v2** 负责，仅出端口序列化改走 msgspec（C 扩展、免中间
dict）。本模块只放与业务无关的原语（Response + envelope），业务 Struct 镜像放各模块（如
``app/modules/feed/wire.py``）——遵守 import-linter「core 不依赖 modules」。

- ``MsgspecJSONResponse``：starlette ``Response`` 子类，``render`` 直出 ``msgspec.json.encode``
  的 bytes（不再经 stdlib ``json.dumps`` 二次序列化）。
- ``msgspec_ok``：包装与既有 ``err.resp_json`` 同形的 ``{code,msg,data}`` envelope，供开关开启
  时的读热端点直接返回（由 ``err._wrap_result`` 透传，不再二次包装）。
- 编码契约与 Pydantic ``model_dump(mode="json")`` 已实测等价（datetime UTC→``...Z``、naive/带
  偏移、float 最短往返、None、中文 unicode 均一致）；等价性由 ``tests/test_read_msgspec.py`` 守。
"""

from __future__ import annotations

from typing import Any

import msgspec
from starlette.responses import Response

from app.core.err import ERRTABLE, CommonErr
from app.core.logging import get_request_id


class MsgspecJSONResponse(Response):
    """用 msgspec 序列化的 JSON 响应（与 starlette JSONResponse 同 status/media_type 语义）。"""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return msgspec.json.encode(content)


class Envelope(msgspec.Struct):
    """与 ``common.ApiResp`` 同形的成功 envelope（业务读热端点专用）。"""

    code: int
    message: str
    data: Any = None
    request_id: str = ""


def msgspec_ok(data: Any, *, headers: dict[str, str] | None = None) -> Response:
    """构造成功响应（``code/message/data/request_id``），序列化走 msgspec。

    **``data`` 必须已是 msgspec 可编码的值**（本模块/各模块 ``wire.py`` 的 Struct、dict、
    list、str/int/float/bool/None、datetime/UUID/Decimal 等）。Pydantic 模型实例、任意对象、
    ``set``、``bytes`` 不在此列：``msgspec.json.encode`` 会抛 TypeError，而 ``err._wrap_result``
    对已是 Response 的返回值直接透传、不会兜住它 → 读热端点会变成未处理的 500。
    放宽的办法是给 Encoder 配 ``enc_hook``（回落 ``fastapi.encoders.jsonable_encoder``），
    但那会在个别类型上偏离 Pydantic ``model_dump(mode="json")`` 的口径，破坏本模块 docstring
    承诺的「两条序列化路径 JSON 等价」，故这里用明确的类型契约约束调用方。
    """
    status, msg = ERRTABLE[CommonErr.OK]
    return MsgspecJSONResponse(
        status_code=status,
        content=Envelope(
            code=int(CommonErr.OK), message=msg, data=data, request_id=get_request_id()
        ),
        headers=headers,
    )
