"""配置项元数据：界面据此生成设置菜单、校验输入、格式化显示。

**为什么单独一张表**

配置从「手写 YAML」改成「程序内调」之后，界面需要知道：

- 有哪些可调项、分组怎么分；
- 每项的**取值范围**（输入 0 或 999999 时要挡住）；
- 怎么把值显示给人看（``180 秒`` 比 ``180`` 直观）；
- 怎么把用户输入解析回来（``3 分钟`` / ``180`` 都要能接受）。

把这些集中在一张表里，界面代码就只剩"渲染 + 收集输入"，不再散布校验逻辑。
新增一个配置项只需要在这里加一行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .app.notices import normalize_points
from .errors import ConfigError

# ---------------------------------------------------------------- 单位

#: 可换算的时间单位 → 折合多少秒。
#:
#: 只在这里出现"秒"这个基准；每个选项自己声明 ``unit``（规范单位，也就是存库
#: 用的那个单位），换算一律走 ``1 单位 = TIME_UNITS[单位] 秒``，不写特例。
TIME_UNITS: dict[str, int] = {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400}


# ---------------------------------------------------------------- 分组


GROUP_BASIC = "基本"
GROUP_REFRESH = "刷新与采集"
GROUP_DETAIL = "详情与提醒"
GROUP_NETWORK = "网络"
GROUP_STORAGE = "存储"

#: 数据目录（``storage.data_dir``）**故意不在这里**：配置本身存在该目录下的
#: meruki.db 里，改它会导致"下次启动找不到配置"，属于鸡生蛋问题。要换目录
#: 得手动搬移整个 data/ 并重建，不适合做成界面开关——不如不给这个入口。
GROUP_LOGIN = "登录"
GROUP_NOTIFY = "通知"

#: 分组展示顺序
GROUP_ORDER = (
    GROUP_BASIC,
    GROUP_REFRESH,
    GROUP_DETAIL,
    GROUP_NETWORK,
    GROUP_STORAGE,
    GROUP_NOTIFY,
    GROUP_LOGIN,
)


@dataclass(frozen=True, slots=True)
class Option:
    """一个可调配置项。"""

    #: 形如 ``notify.points_minutes``
    key: str
    #: 显示名
    label: str
    group: str
    #: 一句话说明（显示在选项下方）
    help: str
    #: 类型：int / float / str / minutes_list
    kind: str
    #: 取值范围（闭区间），None 表示不限制
    minimum: float | None = None
    maximum: float | None = None
    #: 该选项的**规范单位**——存进库里的数值就是这个单位（如 ``秒``）
    unit: str = ""
    #: 可选单位，必须包含 ``unit``；为空表示单位固定、界面上不可切换
    units: tuple[str, ...] = ()
    #: 值为 0 时的说法（``关闭`` / ``不限`` / ``不查证``）；空表示照常显示 0
    zero_text: str = ""
    #: 非时间单位的换算表：``(单位, 1 单位 = 多少规范单位)``。
    #: 时间类走 :data:`TIME_UNITS`，这里只用于比例这类自定义单位（如 ``%``）
    factors: tuple[tuple[str, float], ...] = ()
    #: 允许留空（默认不允许：大多数字符串项留空就是配置错误）。
    #: 典型用法是"可选地址"——默认空着，用户填了才生效。
    allow_empty: bool = False
    #: 敏感值（密码/令牌）：界面上只显示"已设置"，编辑时也不回显明文。
    secret: bool = False
    #: 只在某个开关打开时才出现的行：``("channels.wechat_enable", True)``。
    #: 扫码这种动作用的是还没填的那个渠道，渠道关着时摆一行"扫码挂载"只会
    #: 让人以为点了就能用——干脆不显示（见 :func:`option_visible`）。
    shows_when: tuple[str, object] | None = None

    @property
    def is_action(self) -> bool:
        """动作项（``kind="action"``）：按 Enter 执行，不存值。"""
        return self.kind == "action"

    def read(self, cfg: Any) -> Any:
        """从 Config 读出当前值（支持顶层项如 ``timezone``）。"""
        if self.is_action:
            return ""
        section, _, name = self.key.partition(".")
        if not name:
            return getattr(cfg, section)
        return getattr(getattr(cfg, section), name)

    def write(self, cfg: Any, value: Any) -> Any:
        """写回 Config（dataclass 是 frozen，返回新对象）。"""
        from dataclasses import replace

        if self.is_action:
            # 动作项不落库：执行完由界面自己给反馈
            return cfg
        section, _, name = self.key.partition(".")
        if not name:
            return replace(cfg, **{section: value})
        sub = getattr(cfg, section)
        return replace(cfg, **{section: replace(sub, **{name: value})})

    # ------------------------------------------------------------ 显示

    # ------------------------------------------------------------ 单位换算

    def factor(self, unit: str) -> float:
        """1 个 ``unit`` 等于多少个规范单位；不可换算的单位返回 1.0。"""
        for name, fac in self.factors:
            if name == unit:
                return fac
        canon, other = TIME_UNITS.get(self.unit), TIME_UNITS.get(unit)
        if canon is None or other is None:
            return 1.0
        return other / canon

    def to_canonical(self, value: float, unit: str) -> float:
        """把「以 ``unit`` 计的值」换算成规范单位的值。"""
        return value * self.factor(unit)

    def from_canonical(self, value: float, unit: str) -> float:
        """把规范单位的值换算成「以 ``unit`` 计的值」。"""
        return value / self.factor(unit)

    def display_unit(self, value: Any = None) -> str:
        """挑一个"整好除尽"的最大单位；除不尽就用规范单位。

        ``180 秒`` 显示成 ``3 分钟`` 更直观，但 ``1870 秒`` 不能硬凑成
        ``31.17 分钟``——除不尽的就老实按秒说。
        """
        if self.is_action or not self.units or not value:
            return self.unit
        if isinstance(value, (tuple, list)):
            # 复查点是**一组**数（6 小时 / 1 小时 / 15 分钟），没有"一个"能代表
            # 它们的单位。以前这里拿 tuple 直接除，抛 TypeError 把编辑屏打崩。
            return self.unit
        # 只考虑"换算后是 >=1 的整数"的单位，再取其中最大的那个：
        # 180 秒 → 3 分钟；0.2 倍 → 20%（倍除不尽的小数没法读）；1870 秒 → 秒
        best, best_factor = "", -1.0
        for unit in self.units:
            fac = self.factor(unit)
            scaled = value / fac
            if scaled < 1 or abs(scaled - round(scaled)) > 1e-9:
                continue
            if fac > best_factor:
                best, best_factor = unit, fac
        return best or self.unit

    # ------------------------------------------------------------ 显示

    def format_lines(self, value: Any, *, unit: str | None = None) -> tuple[str, ...]:
        """把值拆成"一行一项"，供设置屏竖排显示。

        复查点这种一组数的项，横着写成 ``6 小时 / 3 小时 / 1 小时 / 15 分钟``
        一长串既难扫也容易撞到右边界被截断（截掉的是最后那个最该看见的点）。
        竖排成
        ::

            - 6 小时
            - 3 小时

        一项一行，永远看得全。其它类型仍是一行（返回单元素元组）。

        顺序：**从远到近**（大数在前），与用户看"还有多久结束"的直觉一致；
        重复的合并掉——手滑写两遍 ``1 小时`` 不该显示两行。
        """
        if self.kind != "minutes_list":
            return (self.format(value, unit=unit),)
        points = normalize_points(value)
        if not points:
            return ("（未设置）",)
        if unit:
            return tuple(f"{self.from_canonical(m, unit):g} {unit}" for m in points)
        return tuple(
            f"{self.from_canonical(m, self.display_unit(m)):g} {self.display_unit(m)}"
            for m in points
        )

    def format(self, value: Any, *, unit: str | None = None) -> str:
        """把值格式化成界面文案；``unit`` 指定显示单位，缺省用规范单位。"""
        if self.is_action:
            # 动作项没有"值"，真正的提示由界面按当前状态给（见 ``render_settings``）
            return "Enter 执行"
        if self.kind == "minutes_list":
            return " / ".join(self.format_lines(value, unit=unit))
        if self.kind == "switch":
            return "开" if value else "关"
        if self.kind in ("int", "float"):
            if not value and self.zero_text:
                return self.zero_text
            shown_unit = unit or self.unit
            shown = self.from_canonical(float(value), shown_unit) if shown_unit else value
            text = f"{shown:g}"
            if not shown_unit:
                return text
            sep = "" if shown_unit == "%" else " "
            return f"{text}{sep}{shown_unit}"
        if self.kind == "str" and not str(value).strip():
            return "（未设置）"
        if self.secret:
            return "（已设置）"
        return str(value)

    # ------------------------------------------------------------ 解析

    def parse(self, text: str, *, unit: str | None = None) -> Any:
        """把用户输入解析成配置值。

        ``unit`` 是用户**按哪个单位输入的**（界面上可切换）；不给就按规范单位。
        换算后的值写回配置，仍用规范单位存储。

        Raises:
            ConfigError: 格式不对或超出范围（消息直接展示给用户）。
        """
        text = text.strip()
        if self.is_action:
            return None
        if self.kind == "minutes_list":
            return self._parse_minutes_list(text, unit=unit)
        if self.kind in ("int", "float"):
            return self._parse_number(text, unit=unit)
        if self.kind == "switch":
            return self._parse_switch(text)
        value: Any = text
        self.validate(value)
        return value

    def _parse_switch(self, text: str) -> bool:
        """开关项的输入解析。

        以前这里直接返回字符串，于是 ``bool("关") == True``——用户明明填了
        "关"，开关反而是开的，而且存进库里的是字符串，下次读配置时就
        "类型不对"起不来。这类"看着生效其实没生效"的配置最坑，所以宁可
        认不出就报错，也不要猜。
        """
        lowered = text.strip().lower()
        if lowered in ("开", "true", "1", "yes", "y", "on", "是", "启用"):
            return True
        if lowered in ("关", "false", "0", "no", "n", "off", "否", "禁用"):
            return False
        raise ConfigError(f"{self.label} 只认开关值（开/关、true/false、1/0），当前填的是 {text}")

    def _parse_minutes_list(self, text: str, *, unit: str | None = None) -> tuple[float, ...]:
        """解析复查点列表，两种写法都认：

        - ``360 / 60 / 15``：每一项按 ``unit``（没给就按规范单位）理解；
        - ``6 小时 / 1 小时 / 15 分钟``：每一项自带单位，逐项换算。

        第二种是 ``format()`` 的显示风格，必须能原样贴回来——以前只认第一种，
        而界面显示的是第二种，于是"照着看到的填"反而报错。
        """
        shown = unit or self.unit or "分钟"
        pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(小时|分钟|分|秒|天)?")
        matches = pattern.findall(text)
        if not matches:
            if text.strip():
                raise ConfigError(
                    f"「{text.strip()}」不是数字；用 / 分隔，例如 6 小时 / 1 小时 / 15 分钟"
                )
            raise ConfigError(f"至少要填一个复查点，例如：6 小时 / 1 小时 / 15 分钟（{shown}）")
        # 数字和单位之外的残留说明有认不出的内容，宁可报错也不静默丢掉一项
        junk = re.sub(r"[/,，、\s]+", "", pattern.sub(" ", text))
        if junk:
            raise ConfigError(f"「{junk}」不是数字；用 / 分隔，例如 6 小时 / 1 小时 / 15 分钟")
        values: list[float] = []
        for number, word in matches:
            num = float(number)
            if num <= 0:
                raise ConfigError(f"复查点必须大于 0 {shown}")
            each = "分钟" if word == "分" else (word or shown)
            converted = self.to_canonical(num, each if each in TIME_UNITS else shown)
            if converted <= 0:
                raise ConfigError(f"「{number} {each}」换算成分钟不足 1 分钟，填大一点")
            values.append(round(converted, 6))
        # 去重并从小到大：内部逻辑假定是"由远及近"
        return tuple(sorted(set(values)))

    def parse_point(self, text: str, *, unit: str | None = None) -> float:
        """解析**一个**复查点（逐条增删用）。

        和 :meth:`_parse_minutes_list` 的区别只在"只认一项"：逐条编辑时每次
        只敲一个数，敲了 ``6 小时 / 1 小时`` 属于没搞清在这个屏里该做什么，
        直接报错比猜"你想加两个"更不容易出错。
        """
        points = self._parse_minutes_list(text, unit=unit)
        if len(points) != 1:
            raise ConfigError(f"这里一次只加一个点，填一个数就好（当前填了 {len(points)} 个）")
        return points[0]

    def _parse_number(self, text: str, *, unit: str | None = None) -> float | int:
        shown = unit or self.unit
        cleaned = text
        for word in TIME_UNITS:
            cleaned = cleaned.replace(word, "")
        cleaned = cleaned.replace("分", "").strip()
        try:
            num = float(cleaned)
        except ValueError:
            raise ConfigError(f"「{text}」不是数字") from None
        converted = self.to_canonical(num, shown) if shown else num
        if self.kind == "int":
            rounded = round(converted)
            if abs(converted - rounded) > 1e-9:
                raise ConfigError(
                    f"{num:g} {shown} = {converted:g} {self.unit}，"
                    f"这一项只能存整数{self.unit}；换个单位或改个数值"
                )
            num = int(rounded)
        else:
            num = round(converted, 6)
        try:
            self.validate(num)
        except ConfigError as exc:
            if shown and shown != self.unit:
                raise ConfigError(f"{exc}（{text} {shown} = {num:g} {self.unit}）") from None
            raise
        return num

    def validate(self, value: Any) -> None:
        """范围校验。

        Raises:
            ConfigError: 超出范围（消息里带上合法区间，方便用户直接改对）。
        """
        if self.is_action:
            return
        if self.kind in ("int", "float") and self.minimum is not None and value < self.minimum:
            raise ConfigError(f"{self.label} 不能小于 {self.minimum:g}，当前填的是 {value}")
        if self.kind in ("int", "float") and self.maximum is not None and value > self.maximum:
            raise ConfigError(f"{self.label} 不能大于 {self.maximum:g}，当前填的是 {value}")
        if self.kind == "str" and not str(value).strip() and not self.allow_empty:
            raise ConfigError(f"{self.label} 不能为空")


# ---------------------------------------------------------------- 选项表

#: 动作项：不是配置值，按 Enter 触发一个流程。放在这里是为了它们能和普通项
#: 一样出现在同一个分组里、享受同一套滚动与分页逻辑。
ACTION_LOGIN = "action.login"
ACTION_TEST_MESSAGE = "action.test_message"
ACTION_MOUNT_WECHAT = "action.mount.wechat"
ACTION_MOUNT_QQ = "action.mount.qq"

OPTIONS: tuple[Option, ...] = (
    Option(
        key=ACTION_LOGIN,
        label="登录挖煤姬",
        group=GROUP_LOGIN,
        kind="action",
        help="走登录流程：浏览器 / 短信 / 手动粘贴 Cookie；已登录时是「更换账号」",
    ),
    Option(
        key="refresh.auto_seconds",
        label="自动刷新间隔",
        group=GROUP_REFRESH,
        kind="int",
        minimum=0,
        maximum=86400,
        help="后台多久自动采集一次；0 = 不自动刷新，只能手动按 r",
        unit="秒",
        units=("秒", "分钟", "小时", "天"),
        zero_text="关闭",
    ),
    Option(
        key="messages.enable",
        label="出价被超过提醒",
        group=GROUP_REFRESH,
        kind="switch",
        help="轮询站内消息：你出过价的拍品被别人超过时立刻拉一次详情（只对出过价的拍品有效）",
    ),
    Option(
        key="translate.enable",
        label="翻译标题与描述",
        group=GROUP_BASIC,
        kind="switch",
        help="列表与详情都显示中文译文（服务端翻译）；译文会存下来，一个拍品只翻一次",
    ),
    Option(
        key="storage.data_stale_minutes",
        label="数据过期提醒",
        group=GROUP_REFRESH,
        kind="int",
        minimum=1,
        maximum=1440,
        help="超过这么久没更新，列表顶部会标「数据已过期」",
        unit="分钟",
        units=("秒", "分钟", "小时"),
    ),
    Option(
        key="detail.enable",
        label="采集详情",
        group=GROUP_DETAIL,
        kind="switch",
        help="关闭后不拉详情：省流量，但拿不到结束时间与当前价",
    ),
    Option(
        key="fetch.min_request_interval",
        label="请求最小间隔",
        group=GROUP_NETWORK,
        kind="float",
        minimum=0,
        maximum=60,
        help="两次请求之间至少隔多少秒；调小容易被风控拦",
        unit="秒",
        units=("秒", "毫秒"),
        factors=(("秒", 1.0), ("毫秒", 0.001)),
    ),
    Option(
        key="fetch.jitter_ratio",
        label="节奏抖动",
        group=GROUP_NETWORK,
        kind="float",
        minimum=0,
        maximum=1,
        help=(
            "请求间隔、自动刷新间隔上下浮动多少，让节奏不像时钟；"
            "0.2 表示 ±20%。以前分三处（队列/刷新/请求）各配一遍，现在只有这一个"
        ),
        unit="倍",
        units=("倍", "%"),
        zero_text="不抖动",
        factors=(("倍", 1.0), ("%", 0.01)),
    ),
    Option(
        key="fetch.timeout_seconds",
        label="单次请求超时",
        group=GROUP_NETWORK,
        kind="int",
        minimum=1,
        maximum=300,
        help="单个请求最多等多少秒",
        unit="秒",
        units=("秒", "分钟"),
    ),
    Option(
        key="fetch.retry",
        label="失败重试次数",
        group=GROUP_NETWORK,
        kind="int",
        minimum=0,
        maximum=10,
        help="网络类失败自动重试几次（指数退避）",
        unit="次",
        zero_text="不重试",
    ),
    Option(
        key="storage.observations_retention_days",
        label="观测保留天数",
        group=GROUP_STORAGE,
        kind="int",
        minimum=1,
        maximum=3650,
        help="历史观测记录保留多少天；越久数据越全，但库越大",
        unit="天",
        units=("天", "小时"),
    ),
    Option(
        key="timezone",
        label="时区",
        group=GROUP_BASIC,
        kind="str",
        help="结束时间按这个时区显示，如 Asia/Shanghai",
    ),
    Option(
        key="login.debug_port",
        label="浏览器调试端口",
        group=GROUP_LOGIN,
        kind="int",
        minimum=1,
        maximum=65535,
        help="浏览器登录用的 CDP 端口；被占用时换一个，如 9333",
    ),
    Option(
        key="login.wait_seconds",
        label="等待登录时长",
        group=GROUP_LOGIN,
        kind="int",
        minimum=0,
        maximum=3600,
        help="拉起浏览器后等多久；0 = 只试一次不等待",
        unit="秒",
        units=("秒", "分钟"),
        zero_text="不等",
    ),
    Option(
        key="login.url",
        label="登录页地址",
        group=GROUP_LOGIN,
        kind="str",
        help="浏览器登录时打开的地址",
    ),
    Option(
        key="notify.outbid_by_price",
        label="用价格判断出价被超过",
        group=GROUP_NOTIFY,
        kind="switch",
        help=(
            "除了站内消息，每轮还用详情里的当前价和你的出价比一次"
            "（当前价高于你的出价就算被超过）；站内消息会漏发，这条不会"
        ),
    ),
    Option(
        key="notify.ended",
        label="拍卖结束提醒",
        group=GROUP_NOTIFY,
        kind="switch",
        help="拍品结束时提醒一次，带上成交价（拿不到成交价就说没成交价，不会不响）",
    ),
    Option(
        key="notify.extended",
        label="截止时间延后提醒",
        group=GROUP_NOTIFY,
        kind="switch",
        help="截止时间被往后推时提醒（雅虎的自动延长也走这条）",
    ),
    Option(
        key="notify.shortened",
        label="截止时间提前提醒",
        group=GROUP_NOTIFY,
        kind="switch",
        help="截止时间被往前挪时提醒——这条和延后含义相反，要赶紧出手",
    ),
    Option(
        key="notify.points_minutes",
        label="结束前复查点",
        group=GROUP_NOTIFY,
        kind="minutes_list",
        help="距结束这些时刻各提醒一次；按 Enter 进去一个点一行地加 / 删",
        unit="分钟",
        units=("分钟", "小时"),
    ),
    Option(
        key="notify.window_minutes",
        label="提醒窗口",
        group=GROUP_NOTIFY,
        kind="int",
        minimum=1,
        maximum=1440,
        help="右侧队列列出未来多少分钟内要发生的事；更远的不显示（到点照样会响）",
        unit="分钟",
        units=("分钟", "小时"),
    ),
    Option(
        key=ACTION_TEST_MESSAGE,
        label="发送测试消息",
        group=GROUP_NOTIFY,
        kind="action",
        help="给已启用的渠道各发一条测试通知，确认真的收得到；没配好的渠道会说明缺什么",
    ),
    Option(
        key="channels.webhook_enable",
        label="Webhook 推送",
        group=GROUP_NOTIFY,
        kind="switch",
        help="到点时 POST 一份 JSON 到下面填的地址，Bark / ntfy / 企微机器人都能用",
    ),
    Option(
        key="channels.webhook_url",
        label="Webhook 地址",
        group=GROUP_NOTIFY,
        kind="str",
        help="启用 Webhook 时必填，http/https 开头的完整地址",
        allow_empty=True,
    ),
    Option(
        key="channels.webhook_timeout_seconds",
        label="Webhook 超时",
        group=GROUP_NOTIFY,
        kind="float",
        minimum=1,
        maximum=60,
        help="推送请求最多等多久，超时就放弃这一条（不影响其它通知）",
        unit="秒",
        units=("秒",),
        zero_text="不等待",
    ),
    Option(
        key="channels.email_enable",
        label="邮件通知",
        group=GROUP_NOTIFY,
        kind="switch",
        help="到点时发一封邮件到下面填的收件人",
    ),
    Option(
        key="channels.email_smtp_host",
        label="SMTP 服务器",
        group=GROUP_NOTIFY,
        kind="str",
        help="如 smtp.qq.com / smtp.gmail.com；启用邮件通知时必填",
        allow_empty=True,
    ),
    Option(
        key="channels.email_smtp_port",
        label="SMTP 端口",
        group=GROUP_NOTIFY,
        kind="int",
        minimum=1,
        maximum=65535,
        help="465 = 隐式 SSL；587/25 = 先明文再尝试 STARTTLS",
    ),
    Option(
        key="channels.email_username",
        label="SMTP 用户名",
        group=GROUP_NOTIFY,
        kind="str",
        help="多数服务商要填完整邮箱地址；留空表示不登录（自建中转）",
        allow_empty=True,
    ),
    Option(
        key="channels.email_password",
        label="SMTP 密码",
        group=GROUP_NOTIFY,
        kind="str",
        help="填的是「授权码」而不是登录密码（QQ / 163 / Gmail 都是）",
        allow_empty=True,
        secret=True,
    ),
    Option(
        key="channels.email_from",
        label="发件人",
        group=GROUP_NOTIFY,
        kind="str",
        help="留空则用 SMTP 用户名",
        allow_empty=True,
    ),
    Option(
        key="channels.email_to",
        label="收件人",
        group=GROUP_NOTIFY,
        kind="str",
        help="多个用逗号分隔；启用邮件通知时必填",
        allow_empty=True,
    ),
    Option(
        key="channels.email_timeout_seconds",
        label="邮件超时",
        group=GROUP_NOTIFY,
        kind="float",
        minimum=1,
        maximum=120,
        help="连 SMTP 到发完最多等多久",
        unit="秒",
        units=("秒",),
    ),
    Option(
        key="channels.onebot_enable",
        label="OneBot 推送",
        group=GROUP_NOTIFY,
        kind="switch",
        help="通过 OneBot v11（QQ 机器人）的 HTTP 接口推一条消息",
    ),
    Option(
        key="channels.onebot_url",
        label="OneBot 地址",
        group=GROUP_NOTIFY,
        kind="str",
        help="HTTP API 根地址，如 http://127.0.0.1:3000",
        allow_empty=True,
    ),
    Option(
        key="channels.onebot_token",
        label="OneBot 令牌",
        group=GROUP_NOTIFY,
        kind="str",
        help="填了就带 Authorization: Bearer <token>；没设就留空",
        allow_empty=True,
        secret=True,
    ),
    Option(
        key="channels.onebot_target",
        label="OneBot 目标",
        group=GROUP_NOTIFY,
        kind="str",
        help="private:QQ号 或 group:群号；只写数字按私聊",
        allow_empty=True,
    ),
    Option(
        key="channels.onebot_timeout_seconds",
        label="OneBot 超时",
        group=GROUP_NOTIFY,
        kind="float",
        minimum=1,
        maximum=120,
        help="调接口最多等多久",
        unit="秒",
        units=("秒",),
    ),
    Option(
        key="channels.wechat_enable",
        label="微信机器人",
        group=GROUP_NOTIFY,
        kind="switch",
        help="打开后这一组里会多出「扫描微信机器人二维码」，扫完就能把提醒发到微信（个人号，腾讯官方 iLink）",
    ),
    Option(
        key="channels.wechat_token",
        label="微信登录凭据",
        group=GROUP_NOTIFY,
        kind="str",
        help="扫码成功后自动填；想在别的机器复用才需要手动填",
        allow_empty=True,
        secret=True,
    ),
    Option(
        key=ACTION_MOUNT_WECHAT,
        label="扫描微信机器人二维码",
        group=GROUP_NOTIFY,
        kind="action",
        help="回车在终端里直接出二维码，手机微信扫一下就换回登录凭据；已经有凭据时按 Enter 只是查看",
        shows_when=("channels.wechat_enable", True),
    ),
    Option(
        key="channels.wechat_base_url",
        label="微信接入点",
        group=GROUP_NOTIFY,
        kind="str",
        help="默认 https://ilinkai.weixin.qq.com；登录后服务端给的地址优先",
        allow_empty=True,
    ),
    Option(
        key="channels.wechat_timeout_seconds",
        label="微信接口超时",
        group=GROUP_NOTIFY,
        kind="float",
        minimum=1,
        maximum=120,
        help="调接口最多等多久（收消息的长轮询另按 45 秒算）",
        unit="秒",
        units=("秒",),
    ),
    Option(
        key="channels.qq_enable",
        label="QQ 机器人",
        group=GROUP_NOTIFY,
        kind="switch",
        help="打开后这一组里会多出「扫描 QQ 机器人二维码」，手机 QQ 扫码选一个机器人即可绑定",
    ),
    Option(
        key="channels.qq_app_id",
        label="QQ 机器人 AppID",
        group=GROUP_NOTIFY,
        kind="str",
        allow_empty=True,
        help="扫码绑定会自动填；也可以手填开放平台给的 AppID",
    ),
    Option(
        key="channels.qq_app_secret",
        label="QQ 机器人 AppSecret",
        group=GROUP_NOTIFY,
        kind="str",
        allow_empty=True,
        secret=True,
        help="扫码绑定会自动填，界面上不回显",
    ),
    Option(
        key=ACTION_MOUNT_QQ,
        label="扫描 QQ 机器人二维码",
        group=GROUP_NOTIFY,
        kind="action",
        help="回车在终端里出二维码，手机 QQ 扫码选一个机器人，直接把 AppID / AppSecret 换回来",
        shows_when=("channels.qq_enable", True),
    ),
    Option(
        key="channels.qq_base_url",
        label="QQ 绑定服务域名",
        group=GROUP_NOTIFY,
        kind="str",
        allow_empty=True,
        help="默认 q.qq.com；官方测试域名是 test.q.qq.com",
    ),
    Option(
        key="channels.qq_api_base",
        label="QQ 接口地址",
        group=GROUP_NOTIFY,
        kind="str",
        allow_empty=True,
        help="默认 https://api.bot.qq.com；本地联调时可指向假服务",
    ),
    Option(
        key="channels.qq_timeout_seconds",
        label="QQ 接口超时",
        group=GROUP_NOTIFY,
        kind="float",
        minimum=1,
        maximum=120,
        help="调开放平台接口最多等多久",
        unit="秒",
        units=("秒",),
    ),
)

#: 按键 -> 选项
OPTIONS_BY_KEY: dict[str, Option] = {o.key: o for o in OPTIONS}


def option_visible(option: Option, cfg: Any) -> bool:
    """这一行在当前配置下是否该出现。

    ``kind="action"`` 的扫码动作挂在对应渠道的开关下面：微信机器人关着时，
    摆一行「扫描微信机器人二维码」只会让人以为点了就能用，实际点进去是
    "还没开机器人渠道"。干脆让它只在开关打开后出现——用户打开开关，
    那一行就长在凭据下面，位置和因果关系都对上了。
    """
    if option.shows_when is None:
        return True
    key, wanted = option.shows_when
    section, _, name = key.partition(".")
    if not name:
        return getattr(cfg, section, None) == wanted
    return getattr(getattr(cfg, section, None), name, None) == wanted


def apply_edit(cfg: Any, key: str, text: str, *, unit: str | None = None) -> Any:
    """按用户输入修改配置，返回新的 Config。

    ``unit`` 是界面上选中的单位；用户填的数字按它解释，落库仍用规范单位。

    Raises:
        ConfigError: 未知项、格式错误或超范围。
        KeyError: 该 key 不是可调项。
    """
    option = OPTIONS_BY_KEY.get(key)
    if option is None:
        raise ConfigError(f"{key} 不是可调配置项")
    value = option.parse(text, unit=unit)
    new_cfg = option.write(cfg, value)
    return revalidate(new_cfg)


def add_point(cfg: Any, key: str, minutes: float) -> Any:
    """往一组数里加一个点（逐条增删用）。

    重复的点直接忽略——它已经在了，再加一遍还要报错只会让人困惑。

    Raises:
        ConfigError: 该 key 不是一组数 / 校验不通过。
        KeyError: 该 key 不是可调项。
    """
    option = OPTIONS_BY_KEY.get(key)
    if option is None:
        raise ConfigError(f"{key} 不是可调配置项")
    if option.kind != "minutes_list":
        raise ConfigError(f"{option.label} 不是一组数")
    current = list(normalize_points(option.read(cfg)))
    if round(minutes, 6) in {round(m, 6) for m in current}:
        return cfg
    current.append(minutes)
    return revalidate(option.write(cfg, tuple(sorted(set(current), reverse=True))))


def drop_point(cfg: Any, key: str, minutes: float) -> Any:
    """从一组数里删掉一个点。

    最后一个点删不掉：整组为空的话配置直接不合法（启动就会报错），
    与其让用户删完再被校验拦住，不如在这一步就说明。

    Raises:
        ConfigError: 该 key 不是一组数，或删完就空了。
        KeyError: 该 key 不是可调项。
    """
    option = OPTIONS_BY_KEY.get(key)
    if option is None:
        raise ConfigError(f"{key} 不是可调配置项")
    if option.kind != "minutes_list":
        raise ConfigError(f"{option.label} 不是一组数")
    current = normalize_points(option.read(cfg))
    kept = tuple(m for m in current if round(m, 6) != round(minutes, 6))
    if len(kept) == len(current):
        return cfg
    if not kept:
        raise ConfigError(f"「{option.label}」至少要留一个点，不能全删")
    return revalidate(option.write(cfg, kept))


def revalidate(cfg: Any) -> Any:
    """改完之后整体校验一遍（跨字段约束如时区合法性）。

    Raises:
        ConfigError: 校验不通过。
    """
    from .config import validate_config

    return validate_config(cfg)


__all__ = [
    "GROUP_ORDER",
    "OPTIONS",
    "OPTIONS_BY_KEY",
    "TIME_UNITS",
    "Option",
    "add_point",
    "apply_edit",
    "drop_point",
    "option_visible",
]
