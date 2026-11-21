"""翻译编排：什么时候该翻、翻什么、翻完存哪。

界面不该自己去拼"这条有没有译文、要不要发请求"——那是 core 的事，TUI 只问
"给我这个名字的显示文本"。

三条规则决定了它的形状：

1. **只翻需要的**。已有译文的直接用缓存，不重复打接口（翻译是网络请求，
   列表十几条每轮重翻就是十几倍请求量）。
2. **失败不写空壳**。接口翻不出来时不落库，否则下次会以为"已翻译"而不再重试。
3. **翻不出来不算错**。翻译是锦上添花，失败了照常显示原文，不能阻塞刷新。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from ..adapters.store.translations import Translation, TranslationStore
from ..domain.goods import clip_for_translation
from ..errors import MerukiTrackerError
from ..logging_setup import get_logger

log = get_logger(__name__)

#: 默认目标语言。挖煤姬接口实测支持 ``zh-CN`` / ``zh-TW`` / ``en``。
DEFAULT_TARGET = "zh-CN"
#: 商品文案基本都是日文。
DEFAULT_SOURCE = "ja"


class TranslatorApi(Protocol):
    """翻译只需要这一个方法（``MerukiApi`` 满足它）。"""

    async def translate(
        self, *, title: str = "", desc: str = "", target: str = ..., source: str = ...
    ) -> tuple[str, str]: ...


@dataclass(frozen=True, slots=True)
class TranslationStats:
    """一次批量翻译的结果。"""

    done: int = 0
    failed: int = 0


class Translator:
    """标题 + 描述的翻译与缓存。"""

    def __init__(
        self,
        api: TranslatorApi,
        store: TranslationStore,
        *,
        target: str = DEFAULT_TARGET,
        source: str = DEFAULT_SOURCE,
    ) -> None:
        self._api = api
        self._store = store
        self._target = target
        self._source = source

    # ---------------------------------------------------------------- 查询

    def cached(self, asin: str) -> Translation | None:
        """取已有译文（没有则 None）。"""
        return self._store.get(asin)

    def cached_all(self) -> dict[str, Translation]:
        return self._store.all()

    def title_of(self, asin: str, fallback: str) -> str:
        """显示用标题：有译文用译文，否则原文。"""
        hit = self._store.get(asin)
        return hit.title if hit and hit.title else fallback

    def needs(self, asin: str) -> bool:
        """这个拍品还需不需要翻。

        有译文就算不需要——即便标题后来改了也不重翻：重翻的代价是每条拍品每次
        改动都多一次请求，而标题变化对判断"这是什么"影响很小。
        """
        return self._store.get(asin) is None

    # ---------------------------------------------------------------- 执行

    async def translate(self, asin: str, *, title: str = "", desc: str = "") -> Translation | None:
        """翻译一条并落库。失败返回 None（不抛，翻译失败不该打断刷新）。"""
        desc = clip_for_translation(desc)
        try:
            got_title, got_desc = await self._api.translate(
                title=title, desc=desc, target=self._target, source=self._source
            )
        except MerukiTrackerError as exc:
            log.debug("翻译失败 %s: %s", asin, exc)
            return None
        except Exception:  # 翻译是附加功能，任何异常都不该冒到界面上
            log.warning("翻译出现意外错误 %s", asin, exc_info=True)
            return None
        if not got_title and not got_desc:
            return None
        now = datetime.now(UTC)
        self._store.save(
            asin,
            title=got_title,
            desc=got_desc,
            source=self._source,
            target=self._target,
            updated_at=now,
        )
        return Translation(
            asin=asin,
            title=got_title,
            desc=got_desc,
            source=self._source,
            target=self._target,
            updated_at=now.isoformat(),
        )

    async def translate_missing(
        self,
        items: Iterable[tuple[str, str, str]],
        *,
        progress: Callable[[str], None] | None = None,
    ) -> TranslationStats:
        """把 ``(asin, 标题, 描述)`` 里还没有译文的逐条翻掉。

        一条失败不中断：列表里有 10 条，不能因为第 3 条接口抽风就丢下后面 7 条。
        """
        todo = [it for it in items if it[0] and self.needs(it[0])]
        done = failed = 0
        for idx, (asin, title, desc) in enumerate(todo, start=1):
            if progress is not None:
                progress(f"翻译 {idx}/{len(todo)}")
            got = await self.translate(asin, title=title, desc=desc)
            if got is None:
                failed += 1
            else:
                done += 1
        return TranslationStats(done=done, failed=failed)
