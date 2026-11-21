"""刷新编排里「接口镜像 + 逐轮比对」的端到端测试（不触网）。

模拟两次采集：第一轮 3 个拍品，第二轮变成 2 个（少了一个、一个改了标题），
断言镜像表、changes 表、以及快照里的派生结果都对得上。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from meruki_tracker.core.adapters.meruki.api import ListItem, ListPage, MyBidPage
from meruki_tracker.core.adapters.store.credentials import CredentialStore
from meruki_tracker.core.adapters.store.db import connect
from meruki_tracker.core.adapters.store.mirror import MirrorStore
from meruki_tracker.core.adapters.store.observations import ObservationStore
from meruki_tracker.core.adapters.store.snapshot import Snapshot, SnapshotStore
from meruki_tracker.core.app.refresher import Refresher, RefresherOptions
from meruki_tracker.core.domain.diff import KIND_APPEARED, KIND_DISAPPEARED


class _ScriptedApi:
    """按脚本依次返回「进行中」列表页。每次 ``refresh`` 消费一条脚本。

    「已结束」桶单独由 ``ended`` 指定，**不消费脚本**——否则一次结束查证就会
    把后续轮次的脚本挤掉，测试意图全乱。
    """

    def __init__(
        self, rounds: list[tuple[ListItem, ...]], ended: tuple[ListItem, ...] = ()
    ) -> None:
        self._rounds = rounds
        self._ended = ended
        self.calls = 0
        self.statuses: list[str] = []

    async def fetch_list_page(self, page: int, *, status: str = "进行中") -> ListPage:
        self.statuses.append(status)
        if status != "进行中":
            items = self._ended
        else:
            items = self._rounds[self.calls] if self.calls < len(self._rounds) else ()
            self.calls += 1
        return ListPage(
            items=items,
            total_item=len(items),
            current_page=1,
            total_page=1,
            status=status,
            item_per_page=10,
            show_page_num=10,
        )

    async def fetch_my_bids(self, *, status: str = "竞拍中", page: int = 1):
        """镜像测试只关心列表字段，假装我一条都没出过价。"""
        return MyBidPage(items=(), total_item=0, current_page=1, total_page=1, status=status)

    async def aclose(self) -> None:
        return None


def _item(asin: str, name: str) -> ListItem:
    return ListItem(
        asin=asin,
        item_id="1",
        name=name,
        url=f"https://x/{asin}",
        detail_url=f"hex{asin}",
        bid_deadline="2026-12-31 21:00:00",
        bid_deadline_str="1h: 00m: 00s",
        created_time="2026-10-01 10:00:00",
        image_url=f"https://img/{asin}.jpg",
        user_id="9999999",
        raw={"Asin": asin},
    )


def _build(tmp_path: Path, api) -> tuple[Refresher, MirrorStore, SnapshotStore]:
    db = tmp_path / "meruki.db"
    conn = connect(db)
    creds = CredentialStore(db, conn=conn)
    creds.save("sid=test")
    mirror = MirrorStore(db, conn=conn)
    snapshots = SnapshotStore(db, conn=conn)
    # 不再有"首次初始化拉 50 页历史"这件事了：已结束靠拍品从「进行中」滚过去
    # 自然补齐（用户要求「已结束就依赖我们关注中的状态滚动」）。
    refresher = Refresher(
        api=api,
        snapshots=snapshots,
        credentials=creds,
        observations=ObservationStore(db, conn=conn),
        mirror=mirror,
        options=RefresherOptions(detail_enabled=False),
    )
    return refresher, mirror, snapshots


async def test_two_rounds_produce_mirror_and_changes(tmp_path: Path):
    round1 = (_item("A1", "旧名"), _item("A2", "B"), _item("A3", "C"))
    round2 = (_item("A1", "新名"), _item("A2", "B"))
    api = _ScriptedApi([round1, round2])
    refresher, mirror, _ = _build(tmp_path, api)

    snap = Snapshot()
    snap, result = await refresher.refresh(snap)
    assert result.ok, result.error
    assert mirror.count("list_items") == 3
    assert mirror.count("list_pages") == 1

    snap, result = await refresher.refresh(snap)
    assert result.ok, result.error
    # 第二轮完整（total_page=1 且单页），所以消失的 A3 被记下来
    assert mirror.count("list_items") == 2

    recent = mirror.recent_changes(limit=20)
    kinds = [c["kind"] for c in recent]
    assert KIND_DISAPPEARED in kinds
    # 只有第一轮的 3 条算"新出现"；第二轮是消失 1 条 + 标题变化 1 条
    assert sum(1 for k in kinds if k == KIND_APPEARED) == 3
    assert sum(1 for k in kinds if k == KIND_DISAPPEARED) == 1


async def test_mirror_records_every_list_field(tmp_path: Path):
    api = _ScriptedApi([(_item("A1", "拍品"),)])
    refresher, mirror, _ = _build(tmp_path, api)

    await refresher.refresh(Snapshot())

    row = mirror.list_items()[0]
    assert row.image_url == "https://img/A1.jpg"
    assert row.created_time == "2026-10-01 10:00:00"
    assert row.bid_deadline_str == "1h: 00m: 00s"
    assert row.user_id == "9999999"


async def test_mirror_failure_does_not_fail_refresh(tmp_path: Path):
    """镜像只是审计旁证，写不进去不能连累刷新结果。"""
    api = _ScriptedApi([(_item("A1", "x"),)])
    refresher, _, snapshots = _build(tmp_path, api)
    # 把镜像表弄成只读形态：直接换成会抛异常的替身
    refresher._mirror = _BrokenMirror()

    _snap, result = await refresher.refresh(Snapshot())

    assert result.ok is True
    assert snapshots.read() is not None


async def test_missing_mirror_degrades_gracefully(tmp_path: Path):
    """没接镜像层（mirror=None）时行为退化成改造前。"""
    api = _ScriptedApi([(_item("A1", "x"),)])
    refresher, _, snapshots = _build(tmp_path, api)
    refresher._mirror = None

    _snap, result = await refresher.refresh(Snapshot())

    assert result.ok is True
    assert snapshots.read() is not None


class _BrokenMirror:
    """所有方法都炸的镜像替身。"""

    def list_items(self, status=None):
        raise RuntimeError("镜像读不出来")

    def detail_items(self):
        raise RuntimeError("镜像读不出来")

    def save_round(self, **kwargs):
        raise RuntimeError("镜像写不进去")


# ---------------------------------------------------------------- 「已结束」桶不再拉
#
# 以前拍品从「进行中」消失时会去「已结束」翻几页查证。现在结束由**详情**回答
# （详情里有 ``over``），这条旁证路线整套删了——少一次请求，也少一个"翻到第几页
# 算够"的旋钮。下面守着"再也不发那个请求"。


async def test_ended_bucket_is_never_fetched(tmp_path: Path):
    """任何一轮都不该再请求「已结束」列表。"""
    round1 = (_item("A1", "在关注"), _item("A2", "消失的"))
    round2 = (_item("A1", "在关注"),)
    api = _ScriptedApi([round1, round2], ended=(_item("A2", "消失的"),))
    refresher, _, snapshots = _build(tmp_path, api)

    snap = Snapshot()
    snap, result = await refresher.refresh(snap)
    assert result.ok, result.error
    snap, result = await refresher.refresh(snap)
    assert result.ok, result.error

    assert api.statuses.count("已结束") == 0, api.statuses
    # 消失的拍品停在原状态，等详情回答（不是被推断成结束）
    by_asin = {a.asin: a for a in snapshots.read().auctions}
    assert by_asin["A2"].door_state.value == "active"


class _FlakyEndedApi(_ScriptedApi):
    """「已结束」请求一律失败，其余正常。"""

    async def fetch_list_page(self, page: int, *, status: str = "进行中") -> ListPage:
        if status != "进行中":
            from meruki_tracker.core.errors import NetworkError

            self.statuses.append(status)
            raise NetworkError("已结束列表超时")
        return await super().fetch_list_page(page, status=status)


async def test_ended_bucket_is_not_reported_as_disappeared(tmp_path: Path):
    """「已结束」桶里的历史条目不能被算成"这一轮消失了"。

    比对基准必须与本次采集**同一个分桶**。库里同时躺着「已结束」桶（历史上翻到
    的几百条）与「进行中」桶，如果基准不分桶地全读出来，每一轮都会把整个
    「已结束」桶报成 ``disappeared``——实测真实库里是 548 条/轮、248 轮共 13 万行。
    """
    api = _ScriptedApi([(_item("A1", "在关注"),)])
    refresher, mirror, _ = _build(tmp_path, api)

    # 先造一个「已结束」桶（模拟历史遗留的几百条旁证）
    ended_items = [_item(f"E{i}", f"早就结束的 {i}") for i in range(5)]
    mirror.save_round(
        status="已结束",
        pages=[{"status": "已结束", "page": 1, "returned": len(ended_items)}],
        items=[
            {
                "asin": it.asin,
                "item_id": it.item_id,
                "name": it.name,
                "url": it.url,
                "detail_url": it.detail_url,
                "bid_deadline": it.bid_deadline,
                "bid_deadline_str": it.bid_deadline_str,
                "created_time": it.created_time,
                "image_url": it.image_url,
                "user_id": it.user_id,
                "raw": {},
            }
            for it in ended_items
        ],
        seen_at=datetime.now(UTC),
        replace=True,
    )
    assert mirror.count("list_items") == 5

    _snap, result = await refresher.refresh(Snapshot())
    assert result.ok, result.error

    kinds = [c["kind"] for c in mirror.recent_changes(limit=50)]
    assert KIND_DISAPPEARED not in kinds, kinds
    # 「已结束」桶原样留着（5 条历史 + 本轮 1 条「进行中」）
    assert len(mirror.list_items(status="已结束")) == 5
    assert mirror.count("list_items") == 6
    # 「进行中」桶是新的一轮
    assert [it.asin for it in mirror.list_items(status="进行中")] == ["A1"]
