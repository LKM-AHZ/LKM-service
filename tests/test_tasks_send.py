"""任务函数测试：send_code / send_magic_link 委托对应 provider。"""

from typing import Any

from auth import tasks as send


class _Fake:
    def __init__(self) -> None:
        self.codes: list[tuple[str, str]] = []  # (contact, code)
        self.links: list[tuple[str, str]] = []  # (email, link)

    async def send_code(self, contact: str, code: str) -> None:
        self.codes.append((contact, code))

    async def send_magic_link(self, email: str, link: str) -> None:
        self.links.append((email, link))


async def test_send_code_delegates_to_channel(monkeypatch: Any) -> None:
    from auth import channels as ch

    fake = _Fake()
    channel = ch.ContactChannel(
        name="email",
        normalize=ch._email_normalize,
        username_from=ch._email_username_from,
        find_user=None,  # ty: ignore[invalid-argument-type]  # 本测试不用该钩子
        create_verification=None,  # ty: ignore[invalid-argument-type]
        consume_code=None,  # ty: ignore[invalid-argument-type]
        send_code=fake.send_code,
    )
    monkeypatch.setattr(ch, "CHANNELS", {"email": channel})
    await send.send_code("email", "a@b.com", "123456")
    assert fake.codes == [("a@b.com", "123456")]


async def test_send_magic_link_delegates(monkeypatch: Any) -> None:
    from auth import deps

    fake = _Fake()
    monkeypatch.setattr(deps, "get_email_provider", lambda: fake)
    await send.send_magic_link("e@x.com", "https://link")
    assert fake.links == [("e@x.com", "https://link")]
