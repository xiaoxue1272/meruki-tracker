"""TDD：接口给 NaN / inf 的价格不能被当成"有价"记下来。"""

from datetime import UTC, datetime

from meruki_tracker.core.adapters.meruki.api import _as_float, _as_price, _map_detail
from meruki_tracker.core.domain.models import AuctionState, DoorState, EndTime, UserQuery
from meruki_tracker.tui.screens.common import price_text


def test_nan_and_inf_are_not_prices():
    for raw in ("NaN", "nan", "Infinity", "-Infinity", "1e400", "-1e400"):
        assert _as_price(raw) is None, f"{raw} 不该被当成价格"


def test_price_parsing_still_works():
    assert _as_price("1234.5") == 1234.5
    assert _as_price(" 12 ") == 12.0
    assert _as_price("0") is None
    assert _as_price(None) is None
    assert _as_price("不是数字") is None


def test_non_finite_floats_are_not_numbers():
    assert _as_float("NaN") is None
    assert _as_float("1e400") is None
    assert _as_float("12.5") == 12.5


def test_mapped_detail_never_carries_nan_price():
    info = _map_detail(
        {"goodsInfo": {"asin": "A1", "bidPrice": "NaN", "buyNowPrice": "1e400"}, "over": False}
    )
    assert info.bid_price is None
    assert info.buy_now_price is None


def test_price_text_hides_non_finite_crap_from_old_rows():
    now = datetime.now(UTC)
    st = AuctionState(
        asin="A1",
        name="n",
        url="u",
        user_query=UserQuery.FOLLOWING,
        door_state=DoorState.ACTIVE,
        end_time=EndTime.exact(now, now),
        latest_bid=float("nan"),
    )
    assert price_text(st.latest_bid) == "—"
    assert price_text(float("inf")) == "—"
