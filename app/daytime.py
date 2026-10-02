"""工作坊的「一天」怎么切。

单独成一个模块，是因为有两个地方要用同一套定义：

- ``sync``：把提交时间戳归到练习日上，算每天的过题数。
- ``daily``：决定今天是哪一套每日一题、什么时候换题。

这两处必须一致。如果各写各的，就会出现「练习数据算昨天、每日一题算今天」
这种谁也解释不清的错位——用户看着榜单想不明白为什么昨天的题今天还在。

以前只有 ``sync`` 用，常量放在那边没关系；每日一题加进来之后就有了第二
个使用者，再放那边就得靠「记得改两处」来维持一致，那是迟早会出错的做法。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

#: 东八区。
#:
#: 用固定偏移而不是 ``ZoneInfo("Asia/Shanghai")``：服务器上不一定装了
#: tzdata，查不到时区会直接抛异常。中国不实行夏令时，固定 +8 是对的。
CST = timezone(timedelta(hours=8))

#: 一天的起点定在凌晨 4 点而不是 0 点。
#:
#: 比赛经常打到半夜，凌晨一两点交的题在感觉上属于「昨天那一场」。按自然日
#: 切会把这些散到第二天，看上去像是第二天在刷题。定在 4 点，凌晨 0 到 3 点
#: 的记录仍然算前一天。
DAY_BOUNDARY_HOUR = 4

#: 下次换题的时刻也在这个钟点（东八区的 4 点）。
RESET_HOUR = DAY_BOUNDARY_HOUR


def day_of(moment: datetime) -> str:
    """某个时刻属于哪个工作日，返回 YYYY-MM-DD。

    先转成北京时间，再减去 4 小时取日期。于是 0 点到 3 点 59 分归到前一天。
    """
    local = moment.astimezone(CST)
    return (local - timedelta(hours=DAY_BOUNDARY_HOUR)).strftime("%Y-%m-%d")


def day_of_epoch(epoch: int) -> str:
    """epoch 秒 → 工作日的 YYYY-MM-DD。"""
    return day_of(datetime.fromtimestamp(epoch, UTC))


def today() -> str:
    """此刻属于哪个工作日。"""
    return day_of(datetime.now(UTC))


def next_reset(now: datetime | None = None) -> datetime:
    """下一次「换日」的时刻，即东八区凌晨 4 点。

    先算出当前属于哪个工作日，加一天就是下一个换日时刻。这样反推不会
    因为边界判断写错而差一天。
    """
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    current = datetime.strptime(day_of(moment), "%Y-%m-%d")
    # 加一天得到下一个工作日的日期，再钉到 4 点。
    return (current + timedelta(days=1)).replace(tzinfo=CST, hour=RESET_HOUR)


__all__ = [
    "CST",
    "DAY_BOUNDARY_HOUR",
    "RESET_HOUR",
    "day_of",
    "day_of_epoch",
    "next_reset",
    "today",
]
