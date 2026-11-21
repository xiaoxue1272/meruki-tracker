# 上下文速览（给未来的你 / 给 AI）

> 如果你隔了很久再打开这个项目，先读这个文件，3 分钟恢复全部上下文。

---

## 这是什么

**meruki-tracker**（命令 `meruki-tracker`，简写 `mt`）：挖煤姬（meruki.cn）拍卖监视器。

一句话：**告诉你关注的拍卖什么时候结束，并且只在时间确认了之后才显示倒计时。**

前身是 `meruki-auction-monitor`，因为**产品前提错误**（假装时间准确）而推倒重来。

---

## 三条不可动摇的原则

1. **时间只有两态**：`EXACT`（详情确认到秒）/ `PENDING`（还没拿到）。
   **PENDING 绝不显示倒计时** —— 这是代码里的红线测试，不是口号。
2. **状态是二维的**：用户操作态（`following`/`unfollowed`）× 拍卖结果（`active`/`ended`/`invalid`）。
   两维各由各自的权威字段判定、互不推断：`over` 判结束，详情顶层的 `hasCollect`
   判取关（见 §2.2c）。取关判定必须建立在本轮**完整**观测之上。
3. **只有 TUI**：唯一入口 `meruki-tracker`（简写 `mt`），无子命令。
   TUI 是**单个常驻 prompt_toolkit 应用** + 屏幕状态机（列表/详情/登录/设置），
   每屏都是 rich 整屏渲染；不再依赖 questionary。

---

## 当前进度：阶段 1 完整可用

| 模块 | 状态 |
|---|---|
| 文档 01–05 | ✅ v2.0 |
| domain（状态机 + 展示规则） | ✅ 纯函数，覆盖率约 98%（`pytest --cov=meruki_tracker.core.domain` 可复算） |
| 配置（TUI 内设置 + 严格校验，存库；数值项可切单位） | ✅ |
| 协议层（curl 传输 + RPC + 错误分域） | ✅ |
| 列表采集（分页 + 完整性判定） | ✅ |
| 详情采集（精确时间 + 限流） | ✅ **真实联调通过** |
| 存储（`data/meruki.db`：快照/凭证/观测/设置 单库事务） | ✅ |
| TUI（单一常驻应用 + 屏幕状态机：列表/详情/登录/设置/清理） | ✅ |
| 分组视图（关注中/已结束/无法确认/全部）+ 终态清理 | ✅ 「已取关」已取消 |
| 翻译（标题+描述，全局开关，译文落库缓存） | ✅ v6 schema |
| 接口镜像（`list_items`/`list_pages`/`detail_items`/`changes` 全字段同步） | ✅ v2 schema |
| 逐轮比对（`domain/diff.py`）+ 变更事件落库 | ✅ |
| 「结束 vs 取关」服务端查证（「已结束」列表） | ✅ 不再区分，统一判终态 |
| 「已结束」靠「进行中」滚动补齐（不再单独同步历史） | ✅ 四视图同一套刷新 |
| 复活检测（已结束的拍品重新出现在进行中） | ✅ `RESURRECTED` 迁移 |
| 防风控抖动（请求间隔 + 自动刷新间隔） | ✅ |
| 「出价被超过」消息通道（`bid_events` + 优先拉详情） | ✅ v4 schema |
| 通知投递（Webhook / 邮件 / OneBot / 微信 / QQ，配置 → 通知 组） | ✅ 多渠道可同开 |
| 定时刷新 | ✅ 已接入主循环（刷新进行中自动跳过本轮） |

**测试：见下方「怎么跑」；ruff check / format 全过。**

**渲染审计的缺陷已全部修完**（正文保持只读审计快照，修复记录见 [meruki-tracker-docs · 07-渲染审计报告](https://github.com/xiaoxue1272/meruki-tracker-docs/blob/main/07-渲染审计报告.md) 文末附录）：详情屏高度预算、刷新遮罩溢出、页脚极窄丢退出键等，均有回归测试覆盖（`tests/test_screens.py`）。自检屏及其 probe 分页已在 2026-11-09 移除（见 [meruki-tracker-docs · 08-代码质量审计](https://github.com/xiaoxue1272/meruki-tracker-docs/blob/main/08-代码质量审计.md) §9.7），相关审计条目只作历史留档。

---

## 关键技术事实（实测，2026-10-29）

### 1. WAF 会拦截 Python 原生 TLS

`web.meruki.cn` 对 `ssl.wrap_socket` / httpx 的握手**直接超时**（Java 风格指纹拦截），
而系统 curl 使用原生 TLS 栈**正常握手**。

**因此默认传输是 `curl` 子进程**（`transport.mode: curl`），不是 httpx。

### 2. 接口结构

```
列表 POST https://web.meruki.cn/?n=Sig.Front.User.AppYahooAcOrder.CollectionList
  body: {"status": "进行中", "page": 1, "keyword": ""}
  data.Data[]: Asin, Name, Url, DetailUrl(hex编码), Id, BidDeadline, BidDeadlineStr

详情 GET https://web.meruki.cn/?n=Sig.Front.SubSite.AppYahoo.Detail&url=<DetailUrl的hex>
  data.goodsInfo.deadTimeWithZone = "2026-10-29T21:54:40+09:00"   ← 权威时间
  data.goodsInfo.deadDate         = "2026-10-29 20:54:40"         ← 北京时间，无标记
  data.other.deadTime             = "2026-10-29 21:54(日本时间)"  ← 只到分钟，不用
  data.goodsInfo.autoExtend       = true                          ← 会自动延长
  data.other.earlyTermination     = "会"                           ← 可提前结束
  data.over                       = false                          ← 是否已结束
```

**`deadTimeWithZone` 带时区是唯一可靠来源**，优先级最高。

**列表接口按 `status` 分桶，只有两个值是真的**：

| 传的 status | 实际返回 |
|---|---|
| `"进行中"` | 真正的进行中列表 |
| `"已结束"` | 真正的已结束列表 |
| `"全部"` / `"未开始"` / `"已取消"` / 其它 | **静默回落到「已结束」** |

「进行中」`Data[]` 每条 10 个字段：`Asin`、`BidDeadline`（站点本地时间，无时区）、
`BidDeadlineStr`（倒计时文本，如 `"18h: 13m:"`，尾巴可能缺）、`CreatedTime`、
`DetailUrl`（hex）、`Id`（纯数字，**挖煤姬收藏表自增主键**）、`ImageUrl`、`Name`、
`Url`（**明文**详情页 URL，与 `DetailUrl` 解码后一致）、`UserId`（卖家 ID）。顶层还有 `ItemPerPage` / `ShowPageNum` / `CurrentPage` /
`TotalItem` / `TotalPage`。

`Id` 与 `CreatedTime` **严格同序**，且两个 status 沿用同一号段——它代表"关注先后"，
不是按状态分桶的编号。列表接口**不返回价格**（价格只在详情里），别拿它覆盖 `latest_bid`。

`keyword` 是**标题模糊搜索**（`"Nikon"` 在「进行中」收窄到 2 条、「已结束」到 102 条），
但**不搜 `Asin` / `Id`**（传真实 ID 一律零结果）——所以**不存在"按 asin 单查"**，只能翻列表。
零结果时响应只剩 `{ItemPerPage, ShowPageNum, CurrentPage, Data: null}`，
`TotalItem`/`TotalPage` 直接消失——这和"服务端抖动"的形态一样，都会触发协议异常判定。

`Asin` 大多形如 `v1000000001`（1 字母 + 数字），但**约 6% 没有字母前缀**（纯数字，
如 `1000000002`）——别假设 `Asin[0]` 是字母。

**「已结束」按 `Id` 倒序**，最近结束的排最前，所以判断"结束 vs 取关"只需翻前几页。

### 2.1 数据模型：镜像优先，不自己造状态

**本地不维护独立事实，只存接口镜像，变动靠逐轮比对。**

| 表 | 内容 |
|---|---|
| `list_items` | 列表全字段 + `raw_json`；`PRIMARY KEY (status, asin)` 按 status 分桶 |
| `list_pages` | 每页分页元信息 + `returned`（本页实际条数，可与 `ItemPerPage` 对照） |
| `detail_items` | 详情全字段 + `raw_json` |
| `changes` | 逐轮差异事件（`appeared`/`disappeared`/`field_changed`/`detail_refreshed`），**只追加** |
| `auctions` | **派生投影**（TUI 看的视图），不是独立事实来源 |

两条硬约束：

- **列表不完整时必须 `replace=False`**——残缺列表不能当作"其它拍品都不见了"的证据。
- **`bid_deadline_str` 不参与比对**——它是倒计时，每轮都变，比了就是噪音。

### 2.2 结束只由详情判定（2026-11-15 改）

**每轮刷新把关注中的每一条拍品详情都拉一遍**，结束与否只看详情里的 `over`。
用户 m22411 原话：「关注列表去拉取全量的拍品，然后呢，你怎么判断结束，根据我们
现在已有的数据每条挨个去请求详情，从详情判断是否结束。」m22474 进一步确认范围：
「全拉吧，你现在不也是全拉吗？」

这条改动把之前**三条混着用**的路全删了：

| 删掉的 | 原来干什么 |
|---|---|
| `lifecycle.advance` 的 R3/R4 | 不在「进行中」列表里就判 `ENDED`（纯本地推断） |
| `fetch.ended_probe_pages` + `_probe_ended` | 去「已结束」列表翻几页查证 |
| `detail.recheck_before_end_minutes` + `_pick_detail_targets` | 按"距结束多少分钟"挑几条拉详情 |

理由：列表接口只给「进行中」，消失既可能是拍完了也可能是被取关，本地永远猜不准；
「已结束」桶还要考虑"翻到第几页才够"。详情里有 `over`，那是**事实**，一次请求就
能回答，不需要叠加三层启发式。代价是请求量：每轮 = 关注数 次详情请求，所以
`refresh.auto_seconds` 成了唯一的节流旋钮（README 里写清了这个账）。

配套：

- **`lifecycle.advance(old, listed, now)` 收成三参**（`list_complete` 删了）：它现在
  只做 R1 发现 / R2 刷字段 / R5 重新关注 / R6 复活，**不改任何终态**。
  `tests/test_lifecycle.py` 有一条守卫直接 `inspect.getsource(advance)` 断言里面
  不出现 `door_state=DoorState.ENDED`，防止推断悄悄长回来。
- **列表翻到底为止**：`_fetch_bucket(status, max_pages: int | None, progress)`，
  `_fetch_list` 传 `None`。仍然保留"空页 = 到底"和"返回条数 < 期望页大小 = 到底"。
- **`advance` 里 R6 复活照旧**：结束后重新上架、又出现在「进行中」列表里，
  就改回 `ACTIVE` 并清掉旧结束时间。
- **`apply_detail` 补了结束时间**：`detail.ended` 时若详情给的 `end_at` 在**未来**
  （一口价中途买走、卖家提前终止），记成 `now`；已到点或未知则沿用详情给的值。
  以前这段逻辑在 `lifecycle` 里，现在跟着判定一起搬到了 `domain/detail.py`。
- **`status='已结束'` 镜像桶不再写入**：`_write_ended_mirror` 删了。历史那 548 条
  仍在库里（是以前攒的），只是不再新增。
- **退役配置项**：`fetch.list_max_pages`、`fetch.ended_probe_pages`、
  `detail.recheck_before_end_minutes`、`detail.max_recheck_per_cycle` 全部进
  `migrate._OBSOLETE_SETTINGS`——老库残留键不清会让启动直接报「未知配置项」。

### 2.2c 取关只由 `hasCollect` 判定（2026-11-15 补；修掉"永远轮询"漏洞）

§2.2 把"结束"交给详情的 `over`，但当时漏了一件事：**从「进行中」列表消失、
又没结束的拍品**既不满足 `over` 也不满足"仍在关注"，`_needs_detail` 的
`user_query is FOLLOWING` 仍然成立 → 每轮继续拉详情、永远问不出答案。用户配合
做了一次真实取关实验后确认，详情顶层就有答案：

| 样本 | `hasCollect` | 结论 |
|---|---|---|
| 进行中 3 条 / 已结束桶 109 条 | `True` | 仍在关注 |
| 站内搜索从未关注的 23 条 | `False` | 无关商品 |
| 用户刚取消关注的 `1000000004` | `False`（连查 3 次稳定） | 确实取关 |

而且它与 `over` **完全正交**：那条取关的拍品 `over=False`（雅虎又上架了、拍卖
仍在进行），21 条已结束但仍在关注的全是 `True`。全桶翻页（进行中 2 页 /
已结束 55 页）也确认它不在任何收藏桶里。

实现（三处，纯函数链）：

- `core/adapters/meruki/api.py`：`DetailInfo.collected: bool | None`，
  `_map_detail` 用 `_optional_bool(data.get("hasCollect"))`。**三态**：
  `None` = 响应缺字段（老接口 / 异常响应）→ 保持原状态；`False` 才是确实取关；
  `True` 照常。字符串 / 0-1 也认，无法识别一律 `None`。
- `core/domain/detail.py`：`DetailResult.collected` + `apply_detail` 的 **R8**：
  `collected is False and user_query is FOLLOWING` → `UNFOLLOWED` +
  `unfollowed_at=now` + `Transition(UNFOLLOWED)`。R7（结束）与 R8 可同轮发生，
  迁移取其一、状态两维都改。注释写明"这个分支的价值是**停止提问**"。
- `core/app/refresher.py`：`_to_detail_result` 的失败判据加
  `and info.collected is not False`——`hasCollect=False` 本身就是有效结论，
  即使没解析出结束时间也不能当"白拉"，否则取关永远识别不出来。

`_needs_detail` **没改**：`UNFOLLOWED` 自动不再命中，轮询当轮停止。界面也没改：
`views.group_of` 把 UNFOLLOWED 归「已结束」（可按 `x` 清理），
`render.py` 剩余列显示「已离开」、行标记 `·`。**保留行不删**：以后重新关注，
R5 会把它改回 `FOLLOWING`，历史价格曲线还在。

> 后来「无法确认」（`DoorState.INVALID`）又单开了一档视图，见 §2.2e；
> `CLEANABLE` 现在是 `(ENDED, INVALID)`。

> **不能用 `collectInfo` 判**：从未关注过的拍品上它也是非空 dict（实测 23/23），
> 刚取关那条身上同样非空——那是商品信息摘要，不是收藏关系。

### 2.2a 详情里的商品描述与图片

详情接口的原始 JSON（`detail_items.raw_json`）里除了价格还有卖家写的描述和
图片，界面上分两处显示：

- **详情屏**：描述前 3 行（超出提示「按 `v` 看全文」）。
- **不显示图片**：图片地址要登录认证才能访问（复制出去打开是 403），终端也
  放不了图，所以不做——真实反馈：「把图片去了吧，要认证，看不了」。
- **描述全文屏**（`v`）：可滚动，按显示行滚（不是字符偏移——折行后一个逻辑行
  可能占好几行）。

取法（`core/domain/goods.py`，纯函数）：`descText` 优先，缺失时退回剥过标签的
`goodsDesc`（后者带 `<style>` 和 `<SPAN>` 拆字，不剥会在终端里显示一堆尖括号）；
`&nbsp;` 解出来是 U+00A0，要归一成普通空格。

**翻译**（`t`，**全局开关**，列表与详情同时生效）：列表显示中文标题、详情显示
中文标题与描述。走 `Sig.Front.User.Translate.TranslateHtml`，**不需要登录**；
单次上限约 10000 字符，超过服务端直接 500，所以发之前必须截断。

- 开关存在 `settings` 的 `translate.enable`，重启仍生效；列表页和详情页的 `t`
  是同一个开关，设置屏里按 Enter 也能翻面（开关项不弹编辑屏）。
- **译文落库**（`translations` 表，schema v6）：一个拍品只翻一次。列表十几条
  每次启动都重翻就是十几倍请求量，而标题偶尔才改。
- 关掉只是切回原文，缓存保留；再开不用重翻。
- **刷新详情时重翻一次**（`_do_fetch_this_detail` 的成功分支）：卖家改标题是常事，
  缓存里的旧译文不该一直用。
- 编排在 `core/app/translator.py`（`Translator`）：`needs()` 决定要不要翻、
  `translate()` 总是打接口、失败不写空壳（否则下次会以为"已翻译"不再重试）。
  `translate()` 总打接口是有意的——"省请求"的判断在 `needs`/`translate_missing`
  这一层，不是藏在 `translate` 里。

### 2.2b 「已结束」靠滚动补齐，不单独同步

「已结束」视图的内容**全部来自拍品从「进行中」滚过去**（`advance` 判终态），
外加库里已有的记录。不再有"翻 50 页同步历史"这件事——那条链路已删除。

- **四个视图同一套刷新**：关注中 / 已结束 / 无法确认 / 全部按 `r` 都走 `_do_refresh`。
  真实反馈：「已结束就依赖我们关注中的状态滚动，已结束、全部这两个页面的刷新
  都按关注中的刷新逻辑来」。
- **手动与定时不再有区别**（2026-11-15）：`full_details` 这个开关连同
  `_fill_remaining_details` 一起删了。`refresh(previous, progress=None)` 每轮都是
  "全部关注中的拉一遍"，与 `r` 还是定时触发无关。
  `tests/test_refresh_details.py` 锁的是"两轮都全拉"。
- **库里旧的 500+ 条历史已清空**（`drop_ended_history` 一次性迁移，只删
  `auctions` 的终态行，**不动 `observations`**）。理由：那是旧机制灌进来的，
  留着会让「已结束」看起来像一份完整历史、其实再也不会更新。
- 曾经的 `sync_ended` / `_maybe_sync_ended_history` / `merge_ended_history` 函数
  与 `fetch.ended_sync_pages` 配置项都已删除（历史同步这条路整体废弃了，
  函数本身也是死代码）；**老库里的残留键由 `drop_obsolete_settings` 清理**——
  不做这一步程序直接起不来（`Config.from_mapping` 报「未知配置项」）。

**复活**：服务端允许同一条收藏记录在两个桶之间移动（结束后重新上架竞拍，`Id`
不变、位置也不变）。复活必然伴随它出现在「进行中」列表里，所以每次刷新顺手比对
就够——`advance` 的 R6 把「上轮已结束、这轮出现在进行中」判成 `RESURRECTED`，
`door_state` 改回 `active`、结束时间清成 PENDING（留着旧时间会算出负倒计时）。
不这么做的话 R2 只刷字段、不看 `door_state`，它会永远停在「已结束」而实际正在拍卖。

**结束时间与成交价**（两条都只能从别的接口补）：

- 结束时间用列表接口自己给的 `BidDeadline`（站点本地时间，`parse_deadline` 转成
  带时区），存成 `EndTime.exact`。`ENDED` 状态下渲染层不看倒计时
  （`render_remaining` 直接返回"已结束"），所以这个"过去的精确时间"不会误解成
  "还会再结束一次"；它反而是"什么时候结束的"的唯一来源。留 PENDING 会让 500 多条
  历史全挤在同一个哨兵值上、只能按 asin 排——真实反馈："已结束还是之前那点数据"。
- **当前价与一口价分开存、分开显示**（`AuctionState.latest_bid` 与
  `AuctionState.buy_now_price`，schema v5 给 `auctions` 加了 `buy_now_price` 列）。
  一口价商品（ヤフオク 的「即決価格」）没有拍卖价：详情接口的 `bidPrice` 恒为 0，
  真实价格在 `buyNowPrice`。判据严格用 `bidPrice == 0`——普通拍卖即使无人出价也会给
  起拍价（实测有 6 円的），`isFixedPrice` 那个布尔字段实测不可靠（4 条一口价的
  `isFixedPrice` 全是 `false`）。
  - 真实反馈两次推进：「有些商品价格是0，是因为是一口价商品」→ 先做兜底合并；
    随后「你分开两个字段展示吧，就当前价和一口价」→ 改成两个独立字段。
  - **0 一律当"没有"**（`price_text` / `render._money` 都用 `not value` 判断）：
    接口用 0 表示"无此价"，显示 `¥0` 比 `—` 更误导。
  - 列表列：`wide`（≥96 列）两列都有；`mid`（≥80）只剩当前价；更窄连结束时间都砍。
  - 库里旧数据由 `split_price_fields` 一次性拆回两个字段（判据：镜像表
    `bid_price = 0` 且 `latest_bid` 恰好等于那个 `buy_now_price`，说明是当年兜底
    塞进去的）。
- 价格只能查详情（列表接口不返回价格）。**只有两个入口**：拍品从「进行中」滚进
  终态的**那一轮**补一次成交价（`_fill_ended_details`，量通常 0–2 条）；`Enter` 进
  详情时对单条拉（且只在"还没有任何详情"时自动拉）。
- **不再有批量补价格通道**。曾经有 `fetch_details` / `fetch_details_all`（配
  `PriceScope` 三档与 `detail.ended_price_per_cycle` 配置项），服务于列表上的 `u`
  键；那个键按用户要求移除后，这条链路只剩测试在调，属于"孤儿能力"，已整体删除
  （含配置项；退役键登记在 `_OBSOLETE_SETTINGS`，否则老库残留键会让程序起不来）。
  理由记在这里：*批量拉详情是每个拍品一次请求、最容易招风控的打法*，而它服务的
  需求（"一次把整屏价格补齐"）已由"进详情自动补单条 + 手动刷新连详情一起拉"覆盖。
- 倒计时用**中文单位**（`12分00秒` / `7天 05时07分33秒`），不用 `0:12:00`；
  **前导的 0 单位不写**（`9秒` / `12分00秒` / `5时07分33秒`），但中间的 00 必须留着
  （`1时00分05秒` 不能写成 `1时5秒`，否则像 1:05）。
  `REMAINING_WIDTH = 17`：CJK 每个字占 2 列，无天数 12 列、一位数天 16 列、
  两位数天 17 列——取 17 才装得下 `45天 00时59分59秒`（取 16 时会截断）。
  该列 `justify="right"`，秒数末尾对齐，一眼比得出谁先结束。
- **所有视图**都按结束时间**倒序**（晚的在前）。真实反馈：「所有列表都是按结束时间
  倒序排序」。以前只有「已结束」倒序、关注中是"快结束的优先"，两屏翻页方向相反，
  切视图就得换脑子。没有结束时间（PENDING）的永远排最后。
- 任何视图里**还没有详情**的拍品，按 `Enter` 进详情会**自动**拉一次
  （`Tui._has_no_detail`：`latest_bid is None` **且** `detail_failures == 0`），
  不让用户先自己发现"要按 `r`"。已有详情的进详情只看缓存，想看最新值按 `r`——
  那是后台轮询的活。
- `r` 与定时刷新走**同一条路**（2026-11-15 起每轮都全拉详情，`full_details` 开关已删）；
  刚进详情那条 `_has_no_detail` 的自动补拉仍在（省一次"发现要按 r"的困惑）。
- 详情**全字段**落库：`refresh_detail_of` 走 `MirrorStore.save_details` 写
  `detail_items`（含 `raw_json` 原始响应）。早期只有后台批量采集写镜像，手动看
  详情只写观测，于是"我看过的拍品"在镜像里没有行、原始 JSON 也丢了。
- **老库升级（已随历史同步一起废弃）**：早期版本并入的历史 `end_time` 全是
  PENDING，当时靠 `meta.ended_times_backfilled` 标记补拉一次「已结束」桶回填结束
  时间。历史同步删掉后这段也删了；老库里那批 PENDING 记录由 `drop_ended_history`
  一次性清空，之后靠「进行中」滚动补齐。

### 2.2c 没有任务队列：后台动作直接起协程

2026-11-13 用户拍板（m19845）：「我们现在所有串行的队列逻辑（任务），都去掉」。
于是把 `core/app/queue.py` 的 `TaskQueue` 与 `core/app/user_actions.py` 的
`UserActionRunner` 两层执行器整套删掉，连带删掉 `QueueConfig` / `queue.enable`
配置项（退役键加进 `migrate._OBSOLETE_SETTINGS`，每次启动 DELETE，老库不会报
「未知配置项」）。现在 `Tui._launch(name, make_coro)` 只做两件事：`_spawn()` 起协程、
`_invalidate()` 重画；所有 task 收进 `self._tasks`，`_shutdown()` 统一 cancel。

**为什么可以这么简单**（这是删队列前逐条确认过的，不是拍脑袋）：

- **刷新互斥**由 `refresher.refresh()` 自己的 busy 守卫兜住：第二次进来直接返回
  `正在采集中`。两个刷新并发各拿一份旧快照整份回写、后写盖掉先写（丢价格/丢结束
  时间）的风险本来就不在队列，而在 refresher——守卫已经在了。TUI 在 `r` 上再做
  一道显式提示「已在刷新中，等这一轮结束再按 r」；
- **翻译 / 补详情**写的是内存（`_translations`）或各自的表，不与快照整份覆盖冲突；
- **真正的请求节流**在 HTTP 层由 `MerukiApi._lock` + `_throttle()` 按
  `fetch.min_request_interval` 控制，队列再等 2 秒是重复计时；
- `_auto_loop` 保留 `await asyncio.sleep(jittered_seconds(...))`（每轮重抽抖动）。

所以 `r` / `t` / 短信登录按下就跑、界面立刻有回应，没有 QUEUED/DROPPED 反馈文案，
也没有「任务页」可看。`_busy` 只是进度文案/遮罩，不是按键锁（这点是 2026-11-10
那轮就定下的，删队列后依然成立）。

**界面订阅与侧栏**：`_launch` 起任务即 `_invalidate()` 重画，不碰 `_busy`。右侧侧栏现在是
**「通知队列」**（用户 m14435，2026-11-10 落地）：数据源是
`core/app/notices.py` 的 `NoticeBoard`——A 类「结束前 N 分钟提醒」由每轮刷新后的
`sync_ending()` 按最新快照全量重算（提醒点就是配置里的 `notify.points_minutes`，
**2026-11-13 之前沿用"详情复查点"，用户 m21460 要求分开配；2026-11-14 用户 m21656
又把"开始提醒 + 重复间隔"那套推算换成了显式的点列表；2026-11-15 详情改成每轮全拉后，
详情那组点整个删了，`notify.points_minutes` 是唯一的点列表**；
只有详情确认过的 EXACT 结束时间才注册；程序没在跑时错过的点静默丢弃不补发）；
B 类「出价被超过」由 `_notify_outbid` 注册（按 `msg_id` 去重，注册即到期）；
2026-11-16 起另有**价格/订单状态**这条信号（`_detect_outbid_by_price` →
`add_price_outbid`，按 `("price", asin)` 去重，按「我的出价金额」做跨轮去重），
见 §2.4b。**C 类「事件」**（2026-11-16，用户 m24274）由 `_notify_events` 注册三
种：`KIND_ENDED`（拍卖结束，带成交价）、`KIND_EXTENDED`（截止时间延后）、
`KIND_SHORTENED`（截止时间提前），见 §2.4c。侧栏分两段画：上段是
`now < due_at <= now + notify.window_minutes` 的**将要**发生（`snapshot()`，按触发
时刻升序；窗口以前写死 2 小时、现在 `NoticeBoard.set_window()` 可热改），下段是
`recent()` 的**刚发生**（10 条上限、新的在前）。共用一份行预算，**先砍下段**——
将要的还来得及改变行为。到点的由 TUI 现成的
**1Hz tick** 调 `take_due()` 收走：投递给 `Notifier` + 在 `_notice` 闪一条提示
（`⏰` 结束提醒 / `▲` 出价被超过 / `◆` 事件）。
`Notifier` 由 `Config.channels` 决定（`core/app/notifications.py`）：默认
`NullNotifier` 什么都不发；三个渠道可单开可同开——通用 webhook（POST JSON）、
邮件（SMTP，465 隐式 SSL / 其它端口 STARTTLS，正文 base64 编码）、OneBot v11
（`send_private_msg` / `send_group_msg` + Bearer token）。真正的 IO 都丢到后台
线程，`notify()` 立即返回；渠道异常各自吞掉。密码/令牌在设置屏只显示「已设置」。
tick 只在「刚有通知到点」或「本屏有每秒变化的倒计时」时才重绘，刻意不起
线程/定时器。侧栏在终端 **<80 列自动隐藏**；主体
`_render_body` 建 Console 时必须扣掉侧栏宽（18 列），否则各屏排版按不存在的列数算、
被静默裁掉。
**扫码挂载（2026-11-11 微信 iLink；2026-11-12 挪到配置区并撤掉列表快捷键；
2026-11-13 拆成每渠道一行）**：
`Screen.MOUNT` 属于**配置区**（`_leave_subscreen()` 永远回 `Screen.SETTINGS`），入口只有一条：
「配置 → 通知」组里的两个动作项——`action.mount.wechat`（「扫描微信机器人二维码」，
紧贴「微信登录凭据」下面）与 `action.mount.qq`（「扫描 QQ 机器人二维码」，紧贴
「QQ 机器人 AppSecret」下面）。
**行本身带 `shows_when=("channels.wechat_enable", True)`，渠道开关打开后它才出现**：
拆之前只有一个 `action.mount`，两个渠道都开时 `build_provider()` 静默优先 QQ，
用户点「扫码挂载机器人」根本不知道会扫到哪个；现在点哪行就扫哪个渠道的码
（`_open_mount(kind)` → `build_provider(channels, kind)`，指定渠道没开返回 `None`），
位置本身也说明了"扫的是哪个渠道"。
列表页的 `m` 已经删掉——同一件事两个入口，用户会问"到底哪个才是正经入口"，而配置页那行是
有状态的（`已挂载 888888888` / `Enter 打开二维码`），列表页那个
只是裸跳转。
**已有凭据时不再取码**——`_mount_ready()` 查 `_setting()` 里的
`channels.qq_app_id`/`qq_app_secret`（QQ）或 `channels.wechat_token`（微信），
有就直接显示「已挂载 <号>」，只有显式按 `r` 才 `_open_mount(force=True)` 重取。
这条是为用户报的问题加的：手填完 AppID/AppSecret 还被要求扫码，等于否认他刚做的事。
离开挂载屏永远回配置页（`_mount_from_list` 已随 `m` 一起删掉）：挂载屏只在配置区
存在，回列表等于把人扔到一个没这屏的地方。
`core/app/wechat.py` 是腾讯官方 iLink（微信 ClawBot）协议：`get_bot_qrcode` 拿
`qrcode` + `qrcode_img_content`，`get_qrcode_status` 轮询 `wait → scaned → confirmed`
（`expired` 重取、`scaned_but_redirect` 换 IDC host 继续等），换到 `bot_token` 之后
`getupdates` **长轮询**收消息、`sendmessage` 发消息。`qrlogin.py` 只做一层
`QrProvider` 适配，把登录状态机翻译成挂载屏认识的 `pending/scanned/success/failed`。
二维码在终端里用半块字符画，一个字符格 = 上下两个模块，颜色写死「白底黑块」所以不挑
主题；渲染是无损的（测试把字符反解回矩阵与编码器输出逐位比对），放不下时明说需要
多少列/行而不画半个。
- **发提醒的前提是对方先发过一条消息**：出站必须带 `context_token`，而它只从入站消息
  里来。所以挂载成功后起一个后台循环长轮询收消息，把 `user_id → context_token` 存进
  `wechat_context` 表（重启不丢），`WeChatNotifier` 才能推提醒。
- provider 是**同步**的（HTTP 走 urllib），TUI 用 `asyncio.to_thread` 调，别直接 await。
- **QQ 官方机器人也有扫码**（`core/app/qqbot.py`）：`POST /lite/create_bind_task`
  （带 32 字节随机 `key`）拿 `task_id`，二维码内容是
  `https://q.qq.com/qqbot/openclaw/connect.html?task_id=..&_wv=2`，手机 QQ 扫码后
  **在名下机器人里选一个**；`POST /lite/poll_bind_result` 轮询 `status`
  （0 空 / 1 待扫 / 2 完成 / 3 过期），完成时返回 `bot_appid` 与**加密的**
  `bot_encrypt_secret`——用第 1 步那个 `key` 做 AES-256-GCM 解密（密文布局
  `iv(12)+ct+tag(16)`）才是明文 AppSecret。这条协议是从官方 npm 包
  `@tencent-connect/qqbot-connector` 里读出来的，官方只发了 Node 版。
  key 只留内存、用完即丢，不落盘也不进二维码。两个渠道都开时 `build_provider()`
  优先走 QQ（它的码一步就能换回 AppID/AppSecret）。
- **QQ 消息通道（2026-11-12，`core/app/qqchannel.py`）**：扫码只换来凭据，
  要能推提醒还得连 WebSocket。链路是 `POST /app/getAppAccessToken`（token 缓存到
  过期前 60 秒）→ `GET /gateway` 拿 wss 地址 → Hello → `Identify`（**只订阅
  `1<<25` 群聊与单聊事件**，没申请权限的 intents 会直接被掐断）→ 心跳 → 收
  `C2C_MESSAGE_CREATE`/`GROUP_AT_MESSAGE_CREATE` 把对方 `openid` 写进
  `qq_sessions` 表（`(kind, openid)` 主键，重启不丢）。
  **为什么必须连**：主动消息要知道发给谁，而 `openid` 只出现在用户先说话的那条
  事件里——和微信的 `context_token` 是同一个限制。发消息走
  `POST /v2/users/{openid}/messages`（群是 `/v2/groups/{group_openid}/messages`），
  body `{"content", "msg_type": 0}`；官方出错时也可能回 200 + `code`，所以不能只看
  HTTP 状态。基座是 `https://api.bot.qq.com`（不是旧频道域名 `api.sgroup.qq.com`），
  可用 `channels.qq_api_base` 改，方便本地联调。
- 登录流程保留 `_spawn`（要能 Esc 取消，需要任务句柄），不再有"启动前清掉排队任务"
  这一步——队列整套删掉了。

### 2.2d 没有外层分页：列表 ↔ 配置

用户 2026-11-13 拍板（m19845 / m19861）：「上面的大 tab 列表、通知、任务、配置都不要。
默认就是列表的三个 关注中、已结束、全部。用户按原来的老逻辑，按下 S 键之后，展示的就
变成了配置的信息。然后你再分配置的 tab（可以参考现在的）」。同时「通知功能…压根就全在
配置那里做通知的配置就好了」「任务这个 tab…删掉」。
（引文里的「三个」是当时的状态；后来又单开了「无法确认」一档，见 §2.2e，现在是四个。）

于是：

- `src/meruki_tracker/tui/zones.py` 只剩一个 `next_group(groups, group, delta)`（配置分组
  循环切换）。`Zone` / `NoticeTab` / `ZONE_ORDER` / `LABELS` / `NOTICE_LABELS` /
  `next_zone` / `next_notice_tab` 全删。`Screen` 删 `NOTICES` / `TASKS`。
- `screens/zonetab.py` 只剩 `tab_bar`（配置分组条）；`zone_bar`、整个
  `screens/notices.py`（待发提醒 / 渠道状态两个内层屏）、整个 `screens/tasks.py`
  （自动任务 + 用户操作两段只读屏）全删。`screens/__init__.py` 的对应 re-export 清掉。
- `Tui._draw_bars(console)` **只在 `SETTINGS` / `SETTING_EDIT` 画一根配置分组条**，
  画完照旧 `console.height -= 1`（渲染函数的行预算都按 `console.height` 算，不扣就会
  把页脚顶出屏幕——这个坑反复踩过）。列表页**一根外层条都不画**，视图栏由
  `render_list` 自己负责；`_page_size` 也相应删掉「外层分页条占 1 行」的扣减。
- 键位收口（2026-11-13 第二版）：**"切一个页签"统一用 `Tab` / `Shift+Tab`**，
  左右箭头是同一件事的顺手版本——列表上切四个视图（当时是三个，§2.2e 又加了
  「无法确认」）、配置里切七分组，`[` / `]` 已彻底
  解绑。翻页只剩 `n` / `p`（页数通常只有一页，不值得占最顺手的箭头），只有一页时
  页脚**干脆不显示**「翻页 1/1」。
  实现上 `_bind_nav_keys()` 注册在全局键之后、列表键之前；编辑态的 `Tab`（切单位）
  由 `_bind_settings_keys` 最后注册，prompt_toolkit 同屏同键取最后注册，所以编辑时
  不会误切成下一组。
  进配置页唯一入口是 `s`；`q` / `Esc` 从配置退回列表，列表上 `q` 才退出程序。
- **列表页不再有 `l`**：登录/换号收进「配置 → 登录」动作项，和挂载一样只留一个
  入口（列表页脚因此少占一格）。相关提示语（启动没登录、登录失效、Cookie 抓取失败）
  全部改成"按 `s` 进配置"的说法。
- 通知的节奏是**独立的一节**（`notify.*`，2026-11-13 按用户 m21460 加的；
  2026-11-14 按用户 m21656 改成点列表）：
  `points_minutes`（结束前哪些时刻提醒，默认 `(15, 60, 360)`）、`window_minutes`
  （侧栏窗口，默认 120）。两项摆在「配置 → 通知」组最上面，与渠道配置分开——
  以前提醒频率借用详情复查点，改复查点会莫名改掉提醒。`validate_config` 兜住
  「至少一个点」「每个点 > 0」「window > 0」。

  这组点是**提醒专用**的（2026-11-15 详情改每轮全拉后，详情那组同形的点已删，
  "什么时候拉详情"不再是可配的东西）。
  用户明确要求这组数在设置屏竖排成 `- 6 小时 / - 3 小时 / …`（从远到近、去重），
  所以 `Option.format_lines()` 负责把一组数拆成一行一项，`screens/settings.py`
  的 `_point_bullets()` + `_option_rows()` 让行预算按**真实行数**算（少算一行就
  把固定页脚顶出屏幕）。归一函数是 `notices.normalize_points()`：降序、去重、
  只留正数、上限 `MAX_ENDING_POINTS=60`，refresher 与 TUI 都走它，口径只有一份。

  **2026-11-14 用户 m22010 再加一刀：一组数改成逐条增删**（原话「这种列表能不能
  改成一个一个加一个一个删啊？你现在本质上还是让我写一行然后去解析」）。形态：
  `Screen.POINT_EDIT` 独立一屏，一个点一行，`a` 加、`Enter` 改、`x`/`d` 删、
  `↑↓` 选、`Esc` 两级退出（输入态 → 屏）。和 `SETTING_EDIT`（一屏一输入框）分开
  是刻意的：一是一组数的行数随点数变化，二是"加一个"和"改一个"是不同动作，混在
  一个输入框里又要靠解析猜意图——那正是用户不满的地方。
  数据侧新增三个纯函数（`core/config_options.py`）：`Option.parse_point()`（一次
  只认一个点，多填就报「一次只加一个点」）、`add_point()`（重复直接返回原 cfg）、
  `drop_point()`（删空报「至少要留一个点，不能全删」）。TUI 每步都
  `settings.set()` + `_apply_config()` 立刻生效，**没有保存键**；报错时保留输入态
  和已敲内容（敲错不该白敲）。改一个点时先 `drop_point` 再 `add_point`，保证点的
  个数不变（只改数不该变成"多了一个"）。渲染侧 `render_point_edit()` 自己算
  行预算（`_point_window()` 在 `room` 行内保证 cursor 可见，提示行也计入 room），
  极端矮屏先砍说明、再砍提示条、最后才砍点，一行都放不下时改说「N 个点放不下」。
  改完立刻生效（`_apply_config` 里重算提醒 + 换窗口）。
- 通知配置**全收在「配置 → 通知」组**：渠道开关、地址/凭据、两个扫码动作项、发送测试消息
  都是这一组里的选项/动作项（扫码行靠 `Option.shows_when` 跟着对应渠道开关出现/隐藏，
  `option_visible()` 在 `_visible_options()` 里统一过滤）。不再有"配置里配一半、通知页配一半"，也不再有只读的
  渠道状态页与待发提醒页（右侧窄栏仍是提醒窗口内的摘要，不占页签）。
- 页脚：`s 设置` 与 `q 退出` 在**列表页任何宽度都常显**（`need=0`）。这是新踩到的
  真 bug：拆掉外层 Tab 后 `s 设置` 成了进配置的唯一入口，而极窄档降级原本把它砍了，
  只留 `↑↓ · ←→ · q 退出`。现在极窄档按信息量从多到少试若干候选，
  **≥16 列同时保住 `s 设置` 与 `q 退出`**，再窄才退到只保退出路径。
  原因与 `x 清理` 当年一样：`need` 同时是"显示阈值"和"超宽先丢谁"，给入口键高 need
  反而最先被裁。
- 配置 / 子屏按 `q` 是**退回列表**（走 `_leave_subscreen`），不是退出程序；要退出再按一次。
  页脚据此写「Esc / q 返回列表」，**不写「q 退出」**——写了用户按下去发现没退。
- 分页条里每个标签的**宽度与字重一致**，选中只靠反显（不加粗、未选中不压暗）：
  加粗/压暗会让选中项看着比别的大一圈，切换时整行像在跳。
- 页脚文案与按键必须对得上：只在列表屏绑的键不能写进别的屏页脚；
  设置编辑屏只有该项可选单位时才提示 `Tab 切单位`。
- **每一屏的页脚都要自己按宽度降级**（`_degrade(parts, width=..., fallback=...)`），不能把
  整串丢给 rich 的 `overflow="ellipsis"`：它砍的是尾巴，而被砍掉的往往是「返回列表」这种
  退出提示。降级顺序是"先丢次要项 → 再丢中间项保住首尾 → 最后退到最短提示"。
- 右侧窄栏内容不变（`NoticeBoard` 的提醒窗口摘要，窗口由 `notify.window_minutes`
  决定、默认 120 分钟），窄于 80 列自动隐藏。

### 2.3 防风控

- 抖动只有一个配置项 `fetch.jitter_ratio`（默认 ±20%），请求间隔与自动刷新间隔
  都用它（队列删掉后不再有"任务间隔"这一层）；
- 自动刷新间隔**每轮重新抽一次**，不做固定节奏。

**官方自己也不高频轮询**：详情页 chunk（`DhOsztmF.js`）里 `setInterval` 出现
0 次，唯一的 1 秒循环是本地倒计时（`Ge()`，不发请求）。只在倒计时归零、或
剩余时间正好到 4:00 / 2:00 且 `autoExtend` 为真时 `location.reload()`。
价格进页面读一次就冻结。所以我们的节奏比官方密，几个封顶值别调大：
`fetch.min_request_interval` 建议 ≥1.0；`refresh.auto_seconds` 别调到 2 分钟以下
（现在每轮都要全拉详情）。

### 2.4 「出价被超过」消息通道

服务端会在**你出过价的拍品被别人超过**时写站内消息（标题
`您的拍卖 <12位订单短 ID> 价格被超过`），这是它已经算好的事实。

映射链路（三跳，每一跳都实测过）：

1. `GetMessageWeb`（POST `{"page":1}`）→ `Data[].title` 抠 `\b([A-Z0-9]{12})\b`；
2. `AppYahooAcOrder.Detail`（GET `{"id": <短 ID>}`）→ `data["url"]` 是 **hex 编码**
   的雅虎地址，`bytes.fromhex(...).decode()` 后末段即 asin（`1000000002`）；
3. asin 与 `auctions.asin` 同一体系，直接匹配。

`log` 数组给出订单完整事件流（新到旧，解析后翻转）：`用户取消` → `价格不足` →
`出价成功` → `出价被超过` → `竞拍失败`。**这些都发生在竞拍进行中**，不是结束后
补发（实测 5 个拍品，通知均早于截止 8–29 分钟）。

硬约束：

- **只对出过价的拍品有效**——只关注不出价永远收不到，所以它是额外触发器，
  **不替代轮询**；
- 消息里**没有价格**，只能当触发器（"这个拍品有情况，去拉一次详情"）；
- `GetMessageWeb` **不支持按类型过滤**（传 `name` / `type` 都无效，返回内容与
  不传完全一样），要靠标题自己筛；
- **首次接入不回溯历史**：只把当前最大 `msg_id` 记成水位线起点，否则一上来就
  拉几十个订单详情，纯属自找风控；
- 事件与水位线**必须同一个事务**（先推水位线再写事件，崩在中间会永久丢消息）；
- 消息/订单请求失败**只降级不报错**（"这轮没有消息"），和「已结束」查证同样的态度；
- 一条消息会带出订单的**整条 log**（`用户取消 → 价格不足 → 出价成功 → 出价被超过 →
  竞拍失败`），这些事件共用同一个 `msg_id`。所以 `bid_events` 的唯一索引是
  `(msg_id, event, happened_at)`，**不是 `msg_id`**——只按 `msg_id` 去重会只留下
  第一条，恰好丢掉最该看的「出价被超过」（v4 schema 修的就是这个，实测踩过）。

### 2.4b 「出价被超过」的第二、三条信号：价格与订单状态（2026-11-16）

只用站内消息会漏。实测订单 `DEMOORD00003` 的 log 只有「价格不足」，**没有**
「出价被超过」，而它最终成交价 5001 高于我的出价 5000。所以每轮刷新多拉一次
`AppYahooAcOrder.List {"status":"竞拍中"}`（一次请求覆盖所有进行中的出价），
拿 `JPYPrice`（我的出价）与两条新信号比对：

| 信号 | 判据 | 能覆盖 |
|---|---|---|
| 价格 | 本轮详情的 `bidPrice` > 我的出价 | 在关注、且本轮拉了详情的拍品 |
| 订单状态 | `StatusStr` 含「超过」 | **所有**出过价的拍品，含没关注的 |

三条规则：

1. **价格比对是单向的**：`当前价 > 我的出价` 一定被超过（8 条历史输掉的订单里
   6 条符合、无例外）；`当前价 <= 我的出价` 什么都说明不了——剩下 2 条输了但
   最终价低于我的出价（4100 vs 5000、920 vs 1000），可能是一口价被买走 / 出价被判
   「价格不足」/ 卖家取消。所以只报警，**绝不反推"还领先"**。
2. **价格优先于状态**：有当前价且没被超过时，不采信可能滞后的 `StatusStr`
   （刚加价、订单状态还没更新时会出现这种滞后），避免误报。
3. **跨轮去重按"我的出价金额"**：`self._price_outbid_seen[asin] = mine`。同一个
   出价每轮都满足条件也只响一次；我加了价又被超过，是新事件，会再响；一旦不再被
   超过就清掉记录。

为什么 `reservePrice`（详情顶层）不能当基准：领先时它等于我的出价，但**被别人
超过后变成 0/缺失**（同一条拍品、同一天，订单列表仍是 6000，详情已是 `None`）。
`isAsinActiveByUser` 也不能用——我在拍的拍品上它同样是 `False`。

配置开关 `notify.outbid_by_price`（默认开）；关掉就只剩站内消息那条。
`messages.enable` 管站内消息，两个都能单独关。

### 2.4c 事件类通知：结束 / 时间延后 / 时间提前（2026-11-16）

用户 m24274：「现在通知只有结束前提醒……还应该有结束提醒、拍卖时间延长提醒」。
这三件事**状态机早就算出来了**（`TransitionKind.ENDED` /
`TransitionKind.END_TIME_CHANGED`，后者从 2026-10 起就存在），但一直只用来打一行
日志、加一个计数，从没进过通知板。这一节把 `Transition` 接成第三种通知来源。

**方向不解析文本**：`Transition.detail` 是给日志看的
`"<旧 iso> -> <新 iso>"`，格式随人改。所以 `Transition` 加了
`old_end_at` / `new_end_at` 两个结构化字段，由 `apply_detail` 填，
`_notify_events` 用 `new_at > old_at` 判方向。

**三种 kind**（`core/app/notices.py`）：

| kind | 标题 | 正文 | 去重键 |
|---|---|---|---|
| `KIND_ENDED` | 拍卖结束 | `名称（成交价 ¥1,500）` / 拿不到价格时 `（已结束，没有成交价）` | `(kind, asin, "")` |
| `KIND_EXTENDED` | 截止时间延后 | `名称（11-16 21:06 → 23:06）`；开了自动延长的再加 `，自动延长` | `(kind, asin, 新时间的 iso)` |
| `KIND_SHORTENED` | 截止时间提前 | 同上 | 同上 |

- 结束是终态，`_needs_detail` 不再命中它，天然只响一次；时间变化按新时间去重——
  延到 21:00 和又延到 23:00 是两件事，各响一次。
- 正文时刻用 `SITE_TZ`（+08:00）格式化（`refresher._fmt_clock`）。显示 UTC 会比
  列表里的时间差 8 小时，比不写时间更糟。
- 拿不到成交价也必须响：流拍、卖家取消、详情还没回填价格都属正常，不能因此
  让"它结束了"这件事消失。

**通道与既有两类共存**：`NoticeBoard.add_event()` 注册即到期（`due_at = now`），
下一次 `take_due` 立刻投递；同时 append 进 `_recent`（`deque(maxlen=12)`）供侧栏
回看。`_recent` 故意**不参与** `_all_registered()` 与 `take_due`——它装的是已经收走
的条目，再投递一次就是重复响。

**侧栏两段**（`tui/screens/noticebar.py`）：上段「将要」（`snapshot()`），下段
「── 已发生 ──」（`recent()`，新的在前）。共用一份行预算，**下段先砍**：将要发生
的还来得及让人去加价。下段每条**不带时刻**——都是刚发生的，先后由位置说明；
实测 18 列宽下那 6 列 `HH:MM` 会把正文截成「20:06 时间延 …」，拍品名全丢。

配置：`notify.ended` / `notify.extended` / `notify.shortened`（都是 switch，默认
开，在「配置 → 通知」组）。改完立刻生效（`_apply_config` 直接写
`refresher._notify_*`）。

**「自动延长」那句从哪来（2026-11-16 补）**：`DetailResult` 没有这个字段，
`_notify_events` 改收一份本轮 `detail_rows`（`_fill_details` 刚填好的镜像行，
里面就有 `auto_extend`），建 `asin -> bool` 映射。`kind is KIND_EXTENDED` 且
`auto_extend[asin] is True` 时正文追加 `，自动延长`。提前不可能由自动延长造成，
不标注；缺镜像行时不标注（不猜）。

依据是接口自己的 `other.autoExtendHelp` 原文——"结束前五分钟内当前价格有所上涨，
结束时间将延长五分钟，可重复"。所以这句等于告诉用户"还在有人抬价"，与卖家改
时间区分开。

**`earlyTermination` / `autoExtend` 不是事件（同一批实测的结论）**：两者原始值都是
`会` / `不会`（`other.earlyTerminationHelp` 原文是"指卖家**可以**在拍卖原定结束时间
之前，选择提前结束拍卖"）。41 条真实详情里 `earlyTermination = 会` 有 37 条、其中
17 条**还在进行中**；`autoExtend = 会` 有 34 条。**按"值为真就报警"会天天误报**，
所以它们只做两件事：`autoExtend` 参与延后通知的标注（见上），两者作为拍品属性显示
在详情屏（`render_detail` 的 `auto_extend` / `early_termination` 可选参数，`None`
时不画那一行——"不知道"和"不会"在屏幕上分不清）。真正的"提前终止"表现为 `over`
变 true（→ 结束通知）或结束时间被改早（→ 提前通知），都已有独立通道。

### 3. 登录

未登录返回 `{"code":403,"msg":"请登录"}`。Cookie 存在 `data/meruki.db` 的
`credentials` 表（库文件 0600）。

浏览器登录由程序**自己**启动一个可调试的 Chrome：

- 配置目录 `data/chrome-profile`（持久化，0700）——**独立目录是新版 Chrome
  开启 `--remote-debugging-port` 的硬性要求**，用默认目录会被拒绝；
- 登录完成后经 DevTools 协议（CDP）自动读取 Cookie，拿到即验证，无需粘贴；
- 配置目录持久化 ⇒ 下次运行浏览器里仍是登录态，**启动后约 1 秒内直接捕获**；
- 没装 Chrome 系浏览器时退化为普通打开，并立刻提示改用手动粘贴（不空等）。

两个曾经踩过的坑：

1. `httpx` 默认 `trust_env=True`，会把发往 `127.0.0.1:9222` 的请求交给系统代理，
   拿到代理的 503 —— 本机回环请求必须 `trust_env=False`。
   同一个坑在**标准库**那条路上也有：`urllib.request.urlopen` 尊重系统代理，而 macOS
   代理的排除列表默认**不含 `127.0.0.1`**（`proxy_bypass("127.0.0.1") is False`）。
   所以通知渠道（OneBot / webhook）、扫码网关、QQ / 微信客户端统一走
   `core/net.py` 的 `open_url()`：回环地址用 `ProxyHandler({})` 直连，外网照常走代理。
2. `asyncio.create_subprocess_exec` 的返回对象若被 GC，`SubprocessTransport.__del__`
   会 `close()` 进而 `kill()` 仍在运行的子进程 —— 浏览器会"一闪就关"，
   必须持有进程引用（见 `browser._LAUNCHED`）。

### 2.2e 「无法确认」单独成组、可手动清理（2026-11-15）

用户拍板（m23903）：「如果一直拉不到，那就永久轮训，但是给一个手动可以清理的选项。
或者像你说的，弄一个列，单独成组。」——漏洞 B 的处置。

- **永久轮询不变**：`_needs_detail` 仍是
  `user_query is FOLLOWING and door_state is not ENDED`，`INVALID` 照样每轮拉详情。
  理由：`apply_detail` 成功路径会从 `INVALID` 自己恢复成 `ACTIVE`（R9），
  拉不到往往是临时的，程序不该擅自放弃。
- **界面单开一档**：`views.ListView` 加 `INVALID`、`LABELS[INVALID]="无法确认"`，
  `ORDER=(FOLLOWING, ENDED, INVALID, ALL)`（插在 ENDED 后、ALL 前，不打乱既有 Tab
  节奏）。`group_of(st)` 最优先判 `door_state is INVALID` —— 它和"关不关注""拍没拍完"
  两维都正交，混在「关注中」会淹掉正常条目，藏进「已结束」又等于假装拍完了。
  `counts()` / `next_view()` 遍历 `ORDER` 自动跟着四档走。
- **可清理**：`CLEANABLE=(ENDED, INVALID)`，`x` → Enter 两道确认。
  `render_clean_confirm` 对 INVALID 额外加一句黄色说明：
  「这些拍品还在永久重试：若仍被关注，下轮刷新会重新出现并从零重试；
  清理只清掉当前的失败记录」——因为 `lifecycle.advance` 的 R1 会把仍在「进行中」
  列表里的拍品重新 DISCOVERED，**清理不是永久删除，不能骗用户**。
- `Tui._open_clean_confirm()` 的拒绝文案从写死「只能在「已结束」视图里清理」
  改成按 `CLEANABLE` 动态拼「只能在「已结束」、「无法确认」视图里清理」。
- `render.py` 的 `INVALID_TEXT="无法确认"`、`render_marker` 的 `?`、`marker_style`
  的红色都不用改，本来就有。

---

## 已知问题 / 下一步

阶段 1 已完整交付。进入阶段 2（后台常驻）前需要注意：

1. 观测历史保留天数已读 `storage.observations_retention_days`（默认 90 天）。
2. `meruki-tracker` / `mt` 命令需要 `pip install -e .` 后才在 PATH 里；否则用 `python -m meruki_tracker`。
3. 阶段 2 待做：后台常驻、断线重连、数据过期后的整轮对账。

---

## 数据都放在哪

一个库文件装全部持久状态，没有别的散落文件：

| 位置 | 内容 | 权限 |
|---|---|---|
| `data/meruki.db` | 快照 / Cookie / 观测历史 / 设置 | 0600 |
| `data/chrome-profile/` | 浏览器登录态（持久化，可复用） | 0700 |
| `data/logs/` | 运行日志（已脱敏） | — |

`meruki.db` 的表（schema v6，共 14 张）：

- `meta` —— schema 版本与一次性迁移标记
- `credentials` —— Cookie（单行，`CHECK id = 1`）
- `snapshot` —— 快照元信息（账号、采集时间）
- `auctions` —— 拍品当前状态（**派生投影**，TUI 看的视图，不是事实来源）
- `observations` —— 观测历史（`ix_obs_asin_time`）
- `settings` —— 键值配置，值为 JSON
- `list_items` —— 列表接口全字段 + `raw_json`，`PRIMARY KEY (status, asin)` 按 status 分桶
- `list_pages` —— 每页分页元信息 + `returned`（本页实际条数）
- `detail_items` —— 详情接口全字段 + `raw_json`
- `changes` —— 逐轮差异事件（`appeared`/`disappeared`/`field_changed`/`detail_refreshed`），只追加
- `bid_events` —— 站内消息里解析出的出价事件，唯一索引 `(msg_id, event, happened_at)`
- `translations` —— 译文缓存（一个拍品只翻一次）
- `qq_sessions` —— QQ 机器人收到的 `openid`（`(kind, openid)` 主键）
- `wechat_context` —— 微信机器人的 `user_id → context_token`（发提醒必须带它）

写快照是"事务内删旧行 + 写新行"，提交前失败会回滚——不会出现半新半旧。

三条守则是 2026-11-18 审计补的，都是"不说就没人知道为什么"的那种：

- **读也拿写锁**：`read()` 和 `write()` 抢同一把锁。同一条连接上并发读写能读到事务中间态（实测 2054 次读里 357 次读到 0 条或半份），读极短，串行化代价可以忽略。
- **版本不符拒绝写**：`write()` 在事务里先查 `meta.schema_version`，不符就抛 `SchemaVersionError`。否则"新版本退回旧版本 / 两个版本共用一个库"时，读会失败、上层的空快照又带着 `allow_empty=True` 写回去，一次启动就清空全表。
- **观测逐条提交**：`ObservationStore.add()` 每条自己 `with conn:`。五个 store 共用一条连接，别的 store 的事务失败会回滚整条连接——观测是纯追加历史，被连坐就永远补不回来。

一条脏行也不再拖垮整份快照：时间读不懂按缺失、枚举读不懂按默认并告警，只有 schema 版本不符才整份放弃。

---

## 怎么跑

```bash
cd meruki-tracker
uv venv --python 3.12 && uv pip install -e ".[dev]"
meruki-tracker        # 直接进 TUI（无配置文件，设置存在 data/meruki.db）
#  TUI 内：s 进配置 → 登录（1 浏览器 / 2 手机验证码 / 3 粘贴）→ 回列表 r 拉取
#          Tab / ←→ 切列表四个视图或配置内分组；Esc / q 从子屏退回列表
#          ↑↓ 选择 · n/p 翻页 · Tab 切视图 · Enter 详情 · r 刷新 · s 设置 · q 退出
```

开发验证：
```bash
pytest                                    # 全部通过（含 1 个默认跳过的真实联调用例）
ruff check src tests && ruff format --check src tests
MAM_LIVE_TEST=1 MAM_LIVE_COOKIE='...' pytest tests/test_live.py   # 真实接口
```

---

## 关键文件索引

| 想看什么 | 看哪里 |
|---|---|
| 为什么做这个产品 | [01-产品文档](https://github.com/xiaoxue1272/meruki-tracker-docs/blob/main/01-产品文档.md) |
| 具体要做什么 | [02-需求文档](https://github.com/xiaoxue1272/meruki-tracker-docs/blob/main/02-需求文档.md) |
| 怎么组织代码 | [03-设计文档](https://github.com/xiaoxue1272/meruki-tracker-docs/blob/main/03-设计文档.md) |
| 为什么选这些库 | [04-技术选型文档](https://github.com/xiaoxue1272/meruki-tracker-docs/blob/main/04-技术选型文档.md) |
| 具体怎么写 | [05-详细实现文档](https://github.com/xiaoxue1272/meruki-tracker-docs/blob/main/05-详细实现文档.md) |
| 每个决定怎么来的 | [对话记录](https://github.com/xiaoxue1272/meruki-tracker-docs/blob/main/对话记录.md) |
| 协议真相 | `src/meruki_tracker/core/adapters/meruki/api.py`（RPC 名称与字段名）与 `CONTEXT.md` 上文「接口结构」 |
| 红线在哪 | `tests/test_render.py` |
