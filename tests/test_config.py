"""配置校验与持久化。

配置不再来自外部 YAML 文件，而是：程序内「设置」界面修改 → 存进
``meruki.db`` 的 ``settings`` 表。所以这里测两件事：

1. ``Config.from_mapping`` 的校验（等价于原来对 YAML 的校验）；
2. ``SettingsStore`` 的读写，以及"存 → 读 → 还原成 Config"的往返一致。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from meruki_tracker.core.adapters.store.db import connect as db_connect
from meruki_tracker.core.adapters.store.settings import SettingsStore
from meruki_tracker.core.app.context import _nest, load_config
from meruki_tracker.core.config import (
    Config,
    LoginConfig,
    NotifyConfig,
    StorageConfig,
    validate_config,
)
from meruki_tracker.core.config_options import OPTIONS, OPTIONS_BY_KEY, apply_edit
from meruki_tracker.core.errors import ConfigError


def _cfg(tmp_path: Path) -> Config:
    return replace(Config.defaults(), storage=StorageConfig(data_dir=str(tmp_path / "data")))


# ------------------------------------------------------------------ 默认值


def test_defaults_when_empty():
    cfg = Config.from_mapping({})
    assert cfg.timezone == "Asia/Shanghai"
    assert cfg.transport.mode == "curl"
    assert cfg.notify.points_minutes == (15.0, 60.0, 360.0)
    assert cfg.refresh.auto_seconds == 180


def test_defaults_with_none():
    assert Config.from_mapping(None) == Config.defaults()


# ------------------------------------------------------------------ 校验


def test_unknown_section_key_rejected():
    with pytest.raises(ConfigError, match="未知配置项"):
        Config.from_mapping({"detail": {"nope": 1}})


def test_unknown_top_level_key_rejected():
    with pytest.raises(ConfigError, match="未知配置项"):
        Config.from_mapping({"nope": 1})


def test_wrong_type_rejected():
    with pytest.raises(ConfigError, match="整数"):
        Config.from_mapping({"refresh": {"auto_seconds": "abc"}})


def test_channels_reject_unknown_keys():
    """渠道是类型化配置段了：写错名字要报错，而不是静默忽略。"""
    with pytest.raises(ConfigError, match=r"channels\.x"):
        Config.from_mapping({"channels": {"x": 1}})


def test_root_not_mapping_rejected():
    with pytest.raises(ConfigError, match="映射"):
        Config.from_mapping([1, 2, 3])


# ------------------------------------------------------------------ 时区


def test_invalid_timezone_rejected():
    with pytest.raises(ConfigError, match="时区"):
        Config.from_mapping({"timezone": "Mars/Olympus"})


def test_empty_timezone_rejected():
    with pytest.raises(ConfigError, match="非空"):
        Config.from_mapping({"timezone": ""})


def test_valid_timezone_accepted():
    assert Config.from_mapping({"timezone": "Asia/Tokyo"}).timezone == "Asia/Tokyo"


# ------------------------------------------------------------------ 登录参数


def test_login_defaults():
    login = Config.from_mapping({}).login
    assert login.debug_port == 9222
    assert login.wait_seconds == 180


def test_login_unknown_key_rejected():
    with pytest.raises(ConfigError, match="未知配置项"):
        Config.from_mapping({"login": {"nope": 1}})


@pytest.mark.parametrize("port", [0, 70000, -1])
def test_login_debug_port_out_of_range_rejected(port):
    with pytest.raises(ConfigError, match="debug_port"):
        Config.from_mapping({"login": {"debug_port": port}})


@pytest.mark.parametrize("port", [1, 9222, 65535])
def test_login_debug_port_boundaries_accepted(port):
    assert Config.from_mapping({"login": {"debug_port": port}}).login.debug_port == port


@pytest.mark.parametrize("seconds", [-1, 3601])
def test_login_wait_seconds_out_of_range_rejected(seconds):
    with pytest.raises(ConfigError, match="wait_seconds"):
        Config.from_mapping({"login": {"wait_seconds": seconds}})


def test_login_wait_seconds_zero_means_try_once():
    assert Config.from_mapping({"login": {"wait_seconds": 0}}).login.wait_seconds == 0


@pytest.mark.parametrize(
    "url", ["", "   ", "www.meruki.cn", "ftp://www.meruki.cn", "https://", "://x"]
)
def test_login_url_invalid_rejected(url):
    with pytest.raises(ConfigError, match="url"):
        Config.from_mapping({"login": {"url": url}})


@pytest.mark.parametrize(
    "url",
    ["https://www.meruki.cn/", "http://127.0.0.1:8080/path", "https://meruki.cn/?a=1"],
)
def test_login_url_valid_accepted(url):
    assert Config.from_mapping({"login": {"url": url}}).login.url == url


def test_login_section_fully_parsed():
    cfg = Config.from_mapping(
        {
            "login": {
                "url": "https://a.b/",
                "debug_port": 9333,
                "wait_seconds": 60,
            }
        }
    )
    assert cfg.login == LoginConfig(url="https://a.b/", debug_port=9333, wait_seconds=60)


# ------------------------------------------------------------------ 提醒点


def test_notify_points_must_not_be_empty():
    cfg = replace(Config.defaults(), notify=NotifyConfig(points_minutes=()))
    with pytest.raises(ConfigError, match="提醒点"):
        validate_config(cfg)


def test_notify_points_must_be_positive():
    cfg = replace(Config.defaults(), notify=NotifyConfig(points_minutes=(0, 60)))
    with pytest.raises(ConfigError, match="提醒点"):
        validate_config(cfg)


def test_list_from_json_is_normalized_to_tuple():
    """库里读回来的是 list，Config 必须归一化成 tuple（便于比较与去重）。"""
    cfg = Config.from_mapping({"notify": {"points_minutes": [360, 60, 15]}})
    assert isinstance(cfg.notify.points_minutes, tuple)
    assert cfg.notify.points_minutes == (360.0, 60.0, 15.0)


# ------------------------------------------------------------------ 序列化往返


def test_mapping_roundtrip():
    cfg = Config.defaults()
    assert Config.from_mapping(cfg.to_mapping()) == cfg


def test_mapping_roundtrip_with_custom_values(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg = replace(cfg, timezone="Asia/Tokyo")
    cfg = replace(cfg, notify=NotifyConfig(points_minutes=(120, 30, 5)))
    assert Config.from_mapping(cfg.to_mapping()) == cfg


# ------------------------------------------------------------------ settings 表


def test_settings_roundtrip(tmp_path: Path):
    conn = db_connect(tmp_path / "meruki.db")
    try:
        store = SettingsStore(conn)
        assert store.load_all() == {}
        store.set("refresh.auto_seconds", 600)
        assert store.get("refresh.auto_seconds") == 600
        store.set("notify.points_minutes", [120, 30])
        assert store.get("notify.points_minutes") == [120, 30]
    finally:
        conn.close()


def test_settings_upsert_overwrites(tmp_path: Path):
    conn = db_connect(tmp_path / "meruki.db")
    try:
        store = SettingsStore(conn)
        store.set("timezone", "Asia/Tokyo")
        store.set("timezone", "UTC")
        assert store.get("timezone") == "UTC"
        assert len(store.load_all()) == 1
    finally:
        conn.close()


def test_corrupt_setting_is_skipped_not_fatal(tmp_path: Path):
    """某一项坏了只跳过它，不能让整份配置读不出来（否则程序起不来）。"""
    conn = db_connect(tmp_path / "meruki.db")
    try:
        with conn:
            conn.execute("INSERT INTO settings (key, value) VALUES ('broken', '{not json')")
        store = SettingsStore(conn)
        store.set("timezone", "UTC")
        loaded = store.load_all()
        assert "timezone" in loaded
        assert "broken" not in loaded
    finally:
        conn.close()


def test_load_config_returns_defaults_for_empty_db(tmp_path: Path):
    """空库 → 默认值，但数据目录固定为实际打开的目录（不是 ./data）。"""
    cfg = load_config(tmp_path)
    expected = replace(Config.defaults(), storage=StorageConfig(data_dir=str(tmp_path)))
    assert cfg == expected


def test_load_config_reads_saved_values(tmp_path: Path):
    conn = db_connect(tmp_path / "meruki.db")
    try:
        SettingsStore(conn).set("refresh.auto_seconds", 900)
    finally:
        conn.close()
    assert load_config(tmp_path).refresh.auto_seconds == 900


def test_load_config_rejects_invalid_stored_value(tmp_path: Path):
    """库里的值非法时要明确报错，而不是带着坏配置跑起来。"""
    conn = db_connect(tmp_path / "meruki.db")
    try:
        SettingsStore(conn).set("timezone", "Mars/Olympus")
    finally:
        conn.close()
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_nest_flattens_keys():
    assert _nest({"refresh.auto_seconds": 1, "timezone": "UTC"}) == {
        "refresh": {"auto_seconds": 1},
        "timezone": "UTC",
    }


# ------------------------------------------------------------------ 设置界面的编辑逻辑


def test_apply_edit_changes_value():
    cfg = Config.defaults()
    new = apply_edit(cfg, "refresh.auto_seconds", "600")
    assert new.refresh.auto_seconds == 600
    assert cfg.refresh.auto_seconds == 180, "原对象不该被改（frozen）"


def test_apply_edit_parses_minutes_list():
    """输入顺序无所谓——内部逻辑要求"由远及近"，所以存进去前会升序归一化。"""
    cfg = apply_edit(Config.defaults(), "notify.points_minutes", "120 / 30 / 5")
    assert cfg.notify.points_minutes == (5.0, 30.0, 120.0)


def test_apply_edit_sorts_and_dedupes_points():
    cfg = apply_edit(Config.defaults(), "notify.points_minutes", "15,360,15,60")
    assert cfg.notify.points_minutes == (15.0, 60.0, 360.0)


@pytest.mark.parametrize(
    "key,text,expect",
    [
        ("refresh.auto_seconds", "abc", "数字"),
        ("refresh.auto_seconds", "-5", "不能小于"),
        ("refresh.auto_seconds", "999999", "不能大于"),
        ("timezone", "Mars/Olympus", "时区"),
        ("notify.points_minutes", "abc", "不是数字"),
    ],
)
def test_apply_edit_rejects_bad_input(key, text, expect):
    with pytest.raises(ConfigError, match=expect):
        apply_edit(Config.defaults(), key, text)


def test_apply_edit_interprets_the_selected_unit():
    """同一串数字，单位不同含义不同——单位是输入的一部分。"""
    base = Config.defaults()
    assert apply_edit(base, "refresh.auto_seconds", "2", unit="分钟").refresh.auto_seconds == 120
    assert apply_edit(base, "refresh.auto_seconds", "2", unit="小时").refresh.auto_seconds == 7200
    assert apply_edit(base, "refresh.auto_seconds", "2", unit="秒").refresh.auto_seconds == 2


def test_apply_edit_unit_rounding_refuses_broken_conversion():
    """90 秒换算成分钟不是整数，不能悄悄四舍五入成 2 分钟。"""
    with pytest.raises(ConfigError, match="只能存整数"):
        apply_edit(Config.defaults(), "storage.data_stale_minutes", "90", unit="秒")


def test_percent_unit_keeps_the_ratio_semantics():
    """抖动比例：填 50% 应存成 0.5，而不是 50。"""
    cfg = apply_edit(Config.defaults(), "fetch.jitter_ratio", "50", unit="%")
    assert cfg.fetch.jitter_ratio == 0.5


def test_minutes_list_accepts_hours():
    """复查点按小时填进去，存回来是分钟。"""
    cfg = apply_edit(Config.defaults(), "notify.points_minutes", "6 / 1 / 0.25", unit="小时")
    assert cfg.notify.points_minutes == (15.0, 60.0, 360.0)


def test_display_unit_picks_a_readable_unit():
    """除得尽就用大单位，除不尽就老实按小单位说。"""
    opt = OPTIONS_BY_KEY["refresh.auto_seconds"]
    assert opt.display_unit(180) == "分钟"
    assert opt.display_unit(7200) == "小时"
    assert opt.display_unit(1870) == "秒"
    assert opt.format(180, unit="分钟") == "3 分钟"
    assert opt.format(0) == "关闭"


def test_every_time_option_declares_units_that_convert():
    """时间类选项的单位必须能换算成规范单位，否则切了会算错。"""
    for opt in OPTIONS:
        if opt.kind not in ("int", "float") or not opt.units:
            continue
        assert opt.unit in opt.units, opt.key
        for unit in opt.units:
            assert opt.factor(unit) > 0, (opt.key, unit)
            assert opt.factor(unit) != 1.0 or unit == opt.unit, (opt.key, unit)


def test_apply_edit_unknown_key():
    with pytest.raises(ConfigError, match="不是可调配置项"):
        apply_edit(Config.defaults(), "nope.nope", "1")


def test_every_option_has_unique_key_and_labels():
    keys = [o.key for o in OPTIONS]
    assert len(keys) == len(set(keys)), "配置项 key 不能重复"
    for opt in OPTIONS:
        assert opt.label and opt.help, f"{opt.key} 缺少显示名或说明"


def test_every_option_is_readable_and_formattable():
    cfg = Config.defaults()
    for opt in OPTIONS:
        assert opt.format(opt.read(cfg))


# ------------------------------------------------------------------ 默认值初始化


def test_first_start_seeds_all_defaults(tmp_path: Path):
    """首次启动要把全部默认值写进库，而不是只留空表。

    否则 ``SELECT * FROM settings`` 只是"用户改过的项"，看当前配置得
    "库 + 代码默认值"两头拼，数据库就不是单一真实来源了。
    """
    load_config(tmp_path)

    conn = db_connect(tmp_path / "meruki.db")
    try:
        stored = SettingsStore(conn).load_all()
    finally:
        conn.close()

    expected = Config.defaults().to_flat()
    # 数据目录会被纠正成实际位置（不是默认的 ./data）
    expected["storage.data_dir"] = str(tmp_path)
    assert stored == expected, "库里应当就是完整配置"
    assert stored["refresh.auto_seconds"] == 180
    assert stored["timezone"] == "Asia/Shanghai"


def test_seeding_does_not_overwrite_user_changes(tmp_path: Path):
    conn = db_connect(tmp_path / "meruki.db")
    try:
        SettingsStore(conn).set("refresh.auto_seconds", 900)
    finally:
        conn.close()

    load_config(tmp_path)  # 再次启动

    conn = db_connect(tmp_path / "meruki.db")
    try:
        assert SettingsStore(conn).get("refresh.auto_seconds") == 900
    finally:
        conn.close()


def test_seeding_fills_only_missing_keys(tmp_path: Path):
    """模拟版本升级：新增的配置项要自动补齐，老值不动。"""
    conn = db_connect(tmp_path / "meruki.db")
    try:
        store = SettingsStore(conn)
        store.set("refresh.auto_seconds", 600)
        # 只有一项：其余都是"缺失"的
    finally:
        conn.close()

    load_config(tmp_path)

    conn = db_connect(tmp_path / "meruki.db")
    try:
        stored = SettingsStore(conn).load_all()
    finally:
        conn.close()

    assert stored["refresh.auto_seconds"] == 600, "已有的值不能被覆盖"
    assert stored["fetch.retry"] == 1, "缺的项要补齐"
    assert len(stored) == len(Config.defaults().to_flat())


def test_ensure_defaults_returns_count(tmp_path: Path):
    conn = db_connect(tmp_path / "meruki.db")
    try:
        store = SettingsStore(conn)
        assert store.ensure_defaults({"a": 1, "b": 2}) == 2
        assert store.ensure_defaults({"a": 9, "b": 9, "c": 3}) == 1, "只补缺的那一个"
        assert store.get("a") == 1, "已存在的不覆盖"
    finally:
        conn.close()


def test_to_flat_covers_all_sections():
    flat = Config.defaults().to_flat()
    assert "refresh.auto_seconds" in flat
    assert "storage.data_dir" in flat
    assert "notify.points_minutes" in flat
    assert "timezone" in flat
    assert all("." in k or k == "timezone" for k in flat)


# ------------------------------------------------------------------ 不可调的配置项


def test_data_dir_is_not_editable():
    """数据目录故意不给改。

    配置存在该目录下的 meruki.db 里，改它等于"下次启动找不到配置"——
    鸡生蛋问题。要换目录得手动搬移整个 data/，不该做成界面开关。
    """
    from meruki_tracker.core.config_options import OPTIONS_BY_KEY

    assert "storage.data_dir" not in OPTIONS_BY_KEY, "数据目录不该出现在设置界面"
    with pytest.raises(ConfigError, match="不是可调配置项"):
        apply_edit(Config.defaults(), "storage.data_dir", "/tmp/elsewhere")


def test_data_dir_still_recorded_in_db():
    """虽然不给改，但它仍是完整配置的一部分（写进库便于排查"数据在哪"）。"""
    assert "storage.data_dir" in Config.defaults().to_flat()


def test_stored_data_dir_cannot_redirect_actual_data_dir(tmp_path: Path):
    """数据目录以实际打开的库为准，不能被库里的值反向劫持。

    否则库里只要有一条 storage.data_dir 指向别处，下次启动就会在那边建
    新库——凭证和快照分裂成两份。
    """
    conn = db_connect(tmp_path / "meruki.db")
    try:
        SettingsStore(conn).set("storage.data_dir", "/tmp/somewhere-else")
    finally:
        conn.close()

    cfg = load_config(tmp_path)

    assert cfg.storage.data_dir == str(tmp_path), "必须以实际打开的目录为准"
    assert cfg.data_dir == tmp_path


# ------------------------------------------------------------------ 开关项解析


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("开", True),
        ("关", False),
        ("true", True),
        ("false", False),
        ("on", True),
        ("off", False),
        ("1", True),
        ("0", False),
        ("TRUE", True),
        (" Off ", False),
    ],
)
def test_switch_option_takes_either_language(text: str, want: bool):
    """中文不是硬性要求：英文的 true/false 一样清楚，两套都得认。"""
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import apply_edit

    assert apply_edit(Config.defaults(), "translate.enable", text).translate.enable is want


def test_switch_option_parses_chinese_words():
    """`bool("关")` 是 True——开关项必须自己解析，不能原样存字符串。

    这条是真 bug 的回归：以前 ``parse`` 对 switch 直接返回文本，于是界面填
    "关" 之后配置里存的是字符串 ``"关"``（真值），开关看着没生效，而且落库的
    类型不对，下次读配置会直接起不来。
    """
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import apply_edit

    cfg = apply_edit(Config.defaults(), "messages.enable", "关")
    assert cfg.messages.enable is False

    cfg = apply_edit(cfg, "messages.enable", "开")
    assert cfg.messages.enable is True


def test_switch_option_rejects_garbage():
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import apply_edit
    from meruki_tracker.core.errors import ConfigError

    with pytest.raises(ConfigError):
        apply_edit(Config.defaults(), "messages.enable", "也许")


def test_switch_survives_a_round_trip_through_the_settings_table():
    """开关写库再读回来仍是布尔——否则重启后类型不对，程序起不来。"""
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import apply_edit

    cfg = apply_edit(Config.defaults(), "translate.enable", "开")
    back = Config.from_mapping(cfg.to_mapping())
    assert back.translate.enable is True


# ---------------------------------------------------------------- 通知渠道


def test_channels_survive_a_round_trip():
    """渠道配置要能进 settings 表再读回来（以前 channels 根本没被序列化）。"""
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import apply_edit

    cfg = Config.defaults()
    # 顺序有讲究：开着却缺必填项是非法配置，apply_edit 会当场校验拦住，
    # 所以先把必填项填好、最后再开开关。
    cfg = apply_edit(cfg, "channels.webhook_url", "https://example.com/hook")
    cfg = apply_edit(cfg, "channels.webhook_enable", "开")
    cfg = apply_edit(cfg, "channels.email_smtp_host", "smtp.example.com")
    cfg = apply_edit(cfg, "channels.email_username", "me@example.com")
    cfg = apply_edit(cfg, "channels.email_password", "授权码")
    cfg = apply_edit(cfg, "channels.email_to", "a@example.com, b@example.com")
    cfg = apply_edit(cfg, "channels.email_enable", "开")
    cfg = apply_edit(cfg, "channels.onebot_url", "http://127.0.0.1:3000")
    cfg = apply_edit(cfg, "channels.onebot_target", "group:42")
    cfg = apply_edit(cfg, "channels.onebot_enable", "开")

    flat = cfg.to_flat()
    assert flat["channels.webhook_url"] == "https://example.com/hook"
    assert flat["channels.email_enable"] is True
    assert flat["channels.onebot_target"] == "group:42"

    back = Config.from_mapping(cfg.to_mapping())
    assert back.channels.webhook_enable is True
    assert back.channels.email_enable is True
    assert back.channels.email_smtp_host == "smtp.example.com"
    assert back.channels.email_password == "授权码"
    assert back.channels.email_to == "a@example.com, b@example.com"
    assert back.channels.onebot_enable is True
    assert back.channels.onebot_target == "group:42"


def test_webhook_enabled_requires_a_usable_url():
    from meruki_tracker.core.config import ChannelsConfig, Config, validate_config
    from meruki_tracker.core.errors import ConfigError

    with pytest.raises(ConfigError, match="webhook_url"):
        validate_config(Config(channels=ChannelsConfig(webhook_enable=True, webhook_url="")))

    with pytest.raises(ConfigError, match="webhook_url"):
        validate_config(Config(channels=ChannelsConfig(webhook_enable=True, webhook_url="ftp://x")))

    # 关着的时候留空合法：不是所有人都要推送
    validate_config(Config(channels=ChannelsConfig(webhook_enable=False, webhook_url="")))


def test_webhook_timeout_range():
    from meruki_tracker.core.config import ChannelsConfig, Config, validate_config
    from meruki_tracker.core.errors import ConfigError

    with pytest.raises(ConfigError, match="webhook_timeout_seconds"):
        validate_config(Config(channels=ChannelsConfig(webhook_timeout_seconds=0.2)))
    with pytest.raises(ConfigError, match="webhook_timeout_seconds"):
        validate_config(Config(channels=ChannelsConfig(webhook_timeout_seconds=120)))


def test_email_requires_host_recipient_and_sender():
    from meruki_tracker.core.config import ChannelsConfig, Config, validate_config
    from meruki_tracker.core.errors import ConfigError

    base = {"email_enable": True, "email_to": "a@b.c", "email_from": "me@b.c"}
    with pytest.raises(ConfigError, match="email_smtp_host"):
        validate_config(Config(channels=ChannelsConfig(**base)))

    with pytest.raises(ConfigError, match="email_to"):
        validate_config(
            Config(
                channels=ChannelsConfig(
                    email_enable=True, email_smtp_host="smtp.x", email_from="me@b.c"
                )
            )
        )

    with pytest.raises(ConfigError, match="email_from"):
        validate_config(
            Config(
                channels=ChannelsConfig(
                    email_enable=True, email_smtp_host="smtp.x", email_to="a@b.c"
                )
            )
        )

    # 填了用户名却没填密码/授权码：真跑起来会被服务器拒，提前拦住
    with pytest.raises(ConfigError, match="email_password"):
        validate_config(
            Config(channels=ChannelsConfig(**base, email_smtp_host="smtp.x", email_username="u"))
        )


def test_email_accepts_username_as_sender():
    from meruki_tracker.core.config import ChannelsConfig, Config, validate_config

    validate_config(
        Config(
            channels=ChannelsConfig(
                email_enable=True,
                email_smtp_host="smtp.x",
                email_to="a@b.c",
                email_username="me@b.c",
                email_password="p",
            )
        )
    )


def test_onebot_requires_url_and_valid_target():
    from meruki_tracker.core.config import ChannelsConfig, Config, validate_config
    from meruki_tracker.core.errors import ConfigError

    with pytest.raises(ConfigError, match="onebot_url"):
        validate_config(
            Config(channels=ChannelsConfig(onebot_enable=True, onebot_target="private:1"))
        )

    with pytest.raises(ConfigError, match="onebot_target"):
        validate_config(
            Config(
                channels=ChannelsConfig(
                    onebot_enable=True, onebot_url="http://x", onebot_target="wechat:1"
                )
            )
        )

    with pytest.raises(ConfigError, match="onebot_target"):
        validate_config(
            Config(
                channels=ChannelsConfig(onebot_enable=True, onebot_url="http://x", onebot_target="")
            )
        )


def test_onebot_bare_number_is_private():
    from meruki_tracker.core.config import parse_onebot_target

    assert parse_onebot_target("10086") == ("private", "10086")
    assert parse_onebot_target("private:10086") == ("private", "10086")
    assert parse_onebot_target("group:20086") == ("group", "20086")


def test_secret_options_never_show_the_value():
    """密码/令牌在设置屏上只显示"已设置"，不能把明文摆在屏幕上。"""
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import OPTIONS_BY_KEY

    for key in ("channels.email_password", "channels.onebot_token"):
        opt = OPTIONS_BY_KEY[key]
        assert opt.secret is True
        assert opt.format("hunter2") == "（已设置）"
        assert opt.format("") == "（未设置）"

    # 非敏感项照常显示
    assert OPTIONS_BY_KEY["channels.email_to"].format("a@b.c") == "a@b.c"
    cfg = Config.defaults()
    assert (
        OPTIONS_BY_KEY["channels.email_password"].format(
            OPTIONS_BY_KEY["channels.email_password"].read(cfg)
        )
        == "（未设置）"
    )


def test_removed_system_notification_key_is_cleaned_up(tmp_path):
    """老库里残留 channels.system_enable 时，启动要能自动清掉而不是报错。

    这是真机踩到的：删掉 macOS 渠道后，带着旧键的库直接 ``ConfigError`` 起不来。
    """
    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.adapters.store.migrate import drop_obsolete_settings

    conn = connect(tmp_path / "meruki.db")
    try:
        with conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?)",
                ("channels.system_enable", "true"),
            )
        assert drop_obsolete_settings(conn) == 1
        assert drop_obsolete_settings(conn) == 0, "DELETE 必须幂等"
        row = conn.execute(
            "SELECT COUNT(*) FROM settings WHERE key='channels.system_enable'"
        ).fetchone()
        assert row[0] == 0
    finally:
        conn.close()


# ------------------------------------------------------------------ 配置项收敛


def test_the_three_jitter_knobs_collapsed_into_one():
    """三个抖动（队列/刷新/请求）收敛成一个：``fetch.jitter_ratio``。

    以前三处各存一份同样的 0.2，改一个不生效是必然的坑。
    """
    from meruki_tracker.core.config_options import OPTIONS_BY_KEY

    assert "fetch.jitter_ratio" in OPTIONS_BY_KEY
    for gone in ("queue.jitter_ratio", "refresh.jitter_ratio", "queue.delay_seconds"):
        assert gone not in OPTIONS_BY_KEY


def test_obsolete_jitter_settings_are_dropped_from_an_old_db(tmp_path):
    """老库里留着退役键也不能启动失败：``drop_obsolete_settings`` 先清再解析。"""
    from meruki_tracker.core.adapters.store.db import connect
    from meruki_tracker.core.adapters.store.migrate import drop_obsolete_settings
    from meruki_tracker.core.adapters.store.settings import SettingsStore
    from meruki_tracker.core.app.context import load_config

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = connect(data_dir / "meruki.db")
    store = SettingsStore(conn)
    store.set("queue.jitter_ratio", 0.3)
    store.set("refresh.jitter_ratio", 0.4)
    store.set("queue.delay_seconds", 5)
    store.set("queue.enable", True)
    store.set("login.fallback_to_paste", False)
    conn.commit()
    assert drop_obsolete_settings(conn) == 5
    conn.close()

    cfg = load_config(data_dir)
    assert cfg.fetch.jitter_ratio == Config.defaults().fetch.jitter_ratio


def test_action_items_are_not_config_values():
    """动作项（登录 / 发送测试消息）不存值：写回原配置，读出来是空串。"""
    from meruki_tracker.core.config_options import (
        ACTION_LOGIN,
        ACTION_TEST_MESSAGE,
        OPTIONS_BY_KEY,
        apply_edit,
    )

    cfg = Config.defaults()
    for key in (ACTION_LOGIN, ACTION_TEST_MESSAGE):
        opt = OPTIONS_BY_KEY[key]
        assert opt.is_action
        assert opt.read(cfg) == ""
        assert opt.format("") == "Enter 执行"
        assert apply_edit(cfg, key, "") is cfg


def test_login_action_lives_in_the_login_group():
    from meruki_tracker.core.config_options import ACTION_LOGIN, GROUP_LOGIN, OPTIONS_BY_KEY

    assert OPTIONS_BY_KEY[ACTION_LOGIN].group == GROUP_LOGIN


def test_test_message_action_lives_in_the_notify_group():
    from meruki_tracker.core.config_options import (
        ACTION_TEST_MESSAGE,
        GROUP_NOTIFY,
        OPTIONS_BY_KEY,
    )

    assert OPTIONS_BY_KEY[ACTION_TEST_MESSAGE].group == GROUP_NOTIFY


# ------------------------------------------------------------------ 通知的节奏（m21460）


def test_notify_cadence_lives_in_the_notify_group_with_sane_defaults():
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import GROUP_NOTIFY, OPTIONS_BY_KEY

    for key in ("notify.points_minutes", "notify.window_minutes"):
        assert OPTIONS_BY_KEY[key].group == GROUP_NOTIFY

    cfg = Config.defaults()
    assert cfg.notify.points_minutes == (15.0, 60.0, 360.0)
    assert cfg.notify.window_minutes == 120.0


def test_notify_cadence_round_trips_through_the_settings_table():
    """键是新加的：老库里没有这些行，``ensure_defaults`` 要补上并能读回来。"""
    from meruki_tracker.core.config import Config

    flat = Config.defaults().to_flat()
    assert flat["notify.points_minutes"] == [15.0, 60.0, 360.0]
    rebuilt = Config.from_mapping(
        {"notify": {"points_minutes": (6.0, 15.0, 360.0), "window_minutes": 720.0}}
    )
    assert rebuilt.notify.points_minutes == (6.0, 15.0, 360.0)
    assert rebuilt.notify.window_minutes == 720.0


@pytest.mark.parametrize(
    ("notify", "hint"),
    [
        ({"points_minutes": ()}, "points_minutes"),
        ({"points_minutes": (0.0,)}, "points_minutes"),
        ({"points_minutes": (15.0, -1.0)}, "points_minutes"),
        ({"window_minutes": 0}, "window_minutes"),
    ],
)
def test_notify_cadence_rejects_nonsense(notify, hint):
    from meruki_tracker.core.config import Config, ConfigError, NotifyConfig, validate_config

    with pytest.raises(ConfigError) as err:
        validate_config(replace(Config.defaults(), notify=NotifyConfig(**notify)))
    assert hint in str(err.value)


def test_notify_options_render_in_readable_units():
    from meruki_tracker.core.config_options import OPTIONS_BY_KEY

    points = OPTIONS_BY_KEY["notify.points_minutes"]
    # 竖排：一行一个点，从远到近，重复的合并掉
    assert points.format_lines((6.0, 15.0, 60.0, 180.0, 360.0, 360.0)) == (
        "6 小时",
        "3 小时",
        "1 小时",
        "15 分钟",
        "6 分钟",
    )
    assert points.format((15.0, 60.0)) == "1 小时 / 15 分钟"
    assert points.parse("6 分钟 / 15 分钟 / 1 小时 / 3 小时 / 6 小时") == (
        6.0,
        15.0,
        60.0,
        180.0,
        360.0,
    )


# ------------------------------------------------------------------ 复查点编辑（回归）


def test_recheck_display_unit_does_not_crash_on_a_sequence():
    """``display_unit`` 以前拿 tuple 直接做除法，抛 TypeError 把编辑屏打崩。

    编辑某项时 TUI 会调 ``display_unit(read(cfg))`` 来定默认单位；复查点读出来
    是元组，于是"按 Enter 想改复查点"必然崩——用户报的就是这个。
    """
    opt = OPTIONS_BY_KEY["notify.points_minutes"]
    assert opt.display_unit((360.0, 60.0, 15.0)) == "分钟"
    assert opt.display_unit(()) == "分钟"


def test_recheck_points_can_be_pasted_back_as_displayed():
    """界面显示成 ``6 小时 / 1 小时 / 15 分钟``，用户照着填必须能过。

    以前解析只认 ``360 / 60 / 15``，把显示的样子贴回去会报「「小时」不是数字」。
    """
    opt = OPTIONS_BY_KEY["notify.points_minutes"]
    shown = opt.format((360.0, 60.0, 15.0))
    assert shown == "6 小时 / 1 小时 / 15 分钟"
    assert opt.parse(shown) == (15.0, 60.0, 360.0)


def test_recheck_points_accept_mixed_units():
    opt = OPTIONS_BY_KEY["notify.points_minutes"]
    assert opt.parse("2 小时 / 30 分钟") == (30.0, 120.0)
    assert opt.parse("2小时/30分") == (30.0, 120.0)


def test_recheck_points_still_reject_non_numbers():
    opt = OPTIONS_BY_KEY["notify.points_minutes"]
    with pytest.raises(ConfigError, match="不是数字"):
        opt.parse("abc")
    with pytest.raises(ConfigError, match="复查点"):
        opt.parse("")
    with pytest.raises(ConfigError, match="不是数字"):
        opt.parse("6 小时 abc")


# ------------------------------------------------- 逐条增删（用户 m22010）


def test_point_can_be_parsed_one_at_a_time():
    """逐条编辑时一次只认一个点。

    以前整个列表只认"一行写好几个用 / 隔开"，用户 m22010 说"你现在本质上还是
    让我写一行然后去解析"。逐条编辑屏里敲两个点属于没搞清该做什么，报错比猜好。
    """
    opt = OPTIONS_BY_KEY["notify.points_minutes"]
    assert opt.parse_point("2 小时") == 120.0
    assert opt.parse_point("120") == 120.0
    assert opt.parse_point("1 小时", unit="小时") == 60.0
    with pytest.raises(ConfigError, match="一次只加一个点"):
        opt.parse_point("6 小时 / 1 小时")
    # 空输入走的是"至少要填一个"那条（更具体），不是"填了几个"
    with pytest.raises(ConfigError, match="至少要填一个"):
        opt.parse_point("")


def test_add_point_keeps_the_list_sorted_far_to_near():
    from meruki_tracker.core.config_options import add_point

    cfg = Config.defaults()
    cfg = add_point(cfg, "notify.points_minutes", 180.0)
    assert cfg.notify.points_minutes == (360.0, 180.0, 60.0, 15.0)
    cfg = add_point(cfg, "notify.points_minutes", 720.0)
    assert cfg.notify.points_minutes == (720.0, 360.0, 180.0, 60.0, 15.0)


def test_add_point_ignores_a_duplicate():
    from meruki_tracker.core.config_options import add_point

    cfg = Config.defaults()
    same = add_point(cfg, "notify.points_minutes", 60.0)
    # 已经在里面了：原样返回，不报错也不多一行
    assert same is cfg


def test_drop_point_removes_only_that_one():
    from meruki_tracker.core.config_options import drop_point

    cfg = Config.defaults()
    cfg = drop_point(cfg, "notify.points_minutes", 60.0)
    assert cfg.notify.points_minutes == (360.0, 15.0)
    # 不存在的点：原样返回
    assert drop_point(cfg, "notify.points_minutes", 60.0) is cfg


def test_drop_point_refuses_to_empty_the_list():
    from meruki_tracker.core.config_options import drop_point

    cfg = replace(
        Config.defaults(),
        notify=replace(Config.defaults().notify, points_minutes=(60.0,)),
    )
    with pytest.raises(ConfigError, match="至少要留一个点"):
        drop_point(cfg, "notify.points_minutes", 60.0)


def test_point_helpers_reject_a_non_list_option():
    from meruki_tracker.core.config_options import add_point, drop_point

    cfg = Config.defaults()
    with pytest.raises(ConfigError, match="不是一组数"):
        add_point(cfg, "refresh.auto_seconds", 60.0)
    with pytest.raises(ConfigError, match="不是一组数"):
        drop_point(cfg, "refresh.auto_seconds", 60.0)


# --------------------------------------------------- 事件类通知的三个开关（m24274）


def test_event_notify_switches_live_in_the_notify_group():
    """结束 / 延后 / 提前各一个开关，且都在「通知」组里。"""
    from meruki_tracker.core.config import Config
    from meruki_tracker.core.config_options import GROUP_NOTIFY, OPTIONS_BY_KEY

    for key in ("notify.ended", "notify.extended", "notify.shortened"):
        assert OPTIONS_BY_KEY[key].group == GROUP_NOTIFY
        assert OPTIONS_BY_KEY[key].kind == "switch"

    cfg = Config.defaults()
    assert cfg.notify.ended is True
    assert cfg.notify.extended is True
    assert cfg.notify.shortened is True


def test_event_notify_switches_reach_the_refresher_options():
    """配置要真的传到 RefresherOptions，不能只是界面上有个开关。"""
    from dataclasses import replace

    from meruki_tracker.core.app.refresher import RefresherOptions
    from meruki_tracker.core.config import Config, NotifyConfig

    cfg = replace(
        Config.defaults(),
        notify=NotifyConfig(ended=False, extended=False, shortened=True),
    )
    opts = RefresherOptions.from_config(cfg)

    assert opts.notify_ended is False
    assert opts.notify_extended is False
    assert opts.notify_shortened is True


# --------------------------------------------------- 文档与配置项的一致性


def _readme() -> str:
    return (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_every_setting_key_in_the_readme_table_really_exists():
    """README 的设置项表只能列还活着的键。

    真踩过：``login.fallback_to_paste`` 在"删死配置"那一刀就退役了（启动时
    还会从老库里把它删掉），README 却一直写着它、还给了个默认值。用户照着
    找是找不到的，手写进库里反而会被清掉——文档错比代码错更难发现。
    """
    import re

    readme = _readme()
    keys = [
        m.group(1) for m in re.finditer(r"^\|\s*`([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)`", readme, re.M)
    ]
    assert keys, "README 里应该能扫到设置项表"
    missing = [key for key in keys if key not in OPTIONS_BY_KEY]
    assert not missing, f"README 表格里的这些设置项已经不存在了：{missing}"


def test_readme_does_not_claim_the_ended_bucket_is_synced():
    """「已结束」只由「进行中」滚过来，README 不能再说"翻 50 页同步历史"。

    真踩过：这条链路（``sync_ended`` / ``fetch.ended_sync_pages``）早就删了，
    README 却还写着"首次启动自动拉 500+ 条下来落库、按 ``r`` 只拉这一桶"。
    用户照着按 ``r``，等半天以为程序坏了。
    """
    readme = _readme()
    for phrase in ("拉下来落库", "只拉这一桶"):
        assert phrase not in readme, f"README 里又出现了已删除的同步历史说法：{phrase!r}"


@pytest.mark.parametrize(
    "junk",
    ["abc", "", None, 5, True, 3.5, {"a": 1}, [15, "x"], [None]],
    ids=["字符串", "空串", "null", "整数", "布尔", "浮点", "字典", "混了字符串", "混了 null"],
)
def test_broken_points_minutes_is_a_config_error_not_a_crash(tmp_path: Path, junk: object):
    """``notify.points_minutes`` 存了不是"一串数字"的东西时，必须是 ConfigError。

    这一项在库里是一个数组，而 ``Config.__post_init__`` 会把 list 归一成
    tuple——它对**元素类型**一无所知。于是 ``"abc"`` 被 ``tuple()`` 拆成
    ``('a','b','c')``，一直活到 ``validate_config`` 里 ``p <= 0`` 才炸成裸
    ``TypeError: '<=' not supported between instances of 'str' and 'int'``。
    用户看到的是一个与配置无关的 traceback，而且程序连启动界面都进不去，
    想改都没法改。
    """
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    db = data / "meruki.db"
    conn = db_connect(db)
    try:
        store = SettingsStore(conn)
        store.set("storage.data_dir", str(tmp_path / "data"))
        store.set("notify.points_minutes", junk)
    finally:
        conn.close()

    with pytest.raises(ConfigError) as info:
        load_config(tmp_path / "data")
    assert "notify.points_minutes" in str(info.value)
