"""评审工时时钟（POST /review-clock）。

对 /assign 系列接口产出的「稿件—评审人」配对跟踪评审工时：每个配对声明正整数
工作小时预算，UTC 事件流（接受 accept / 暂停 pause / 恢复 resume / 提交 submit）
按时刻逐对重放推进状态机；只在周一至周五 09:00—17:00 UTC 且非节假日的窗口内
累计工作时长，暂停期间停止计时，跨下班、周末与节假日自动顺延。

配对状态机（事件按时刻排序后重放，全部事件都参与合法性校验）：

    pending --accept--> active --pause--> paused --resume--> active
    active  --submit--> submitted（终态，不再接受任何事件）

请求级非法（接口返回 422）：

- 重复配对：同一（稿件, 评审人）配对声明多次；
- 同对同刻事件：同一配对在同一时刻（不同时区表示的同一瞬间也算）存在多个事件；
- 非法状态转换：不满足上表的事件（如未接受先暂停、暂停中提交、提交后再暂停）；
- 事件引用了未声明的配对。

查询时刻 query_at 之后的事件不参与「查询时刻状态」的计算，但仍参与上述合法性
校验（事件日志整体必须自洽）。报告规则：

- status：查询时刻重放得到的状态（pending / active / paused / submitted）；
- used_work_seconds：查询时刻（submitted 为提交时刻）已累计的工作时长；
- remaining_work_seconds：max(0, 预算 - 已用)；
- deadline_at：仅 active 状态给出——按剩余预算沿工作窗口推算的预计截止 UTC
  时刻（已超预算时为过去某刻）；pending / paused / submitted 均为 null；
- overdue：已用工作时长 > 预算。submitted 按提交时刻判定（提交后不再变化），
  active / paused 按查询时刻判定，pending 恒为 false。

结果按 (稿件 ID, 评审人 ID) 排序；事件输入顺序、节假日重复与乱序均不影响结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

# 工作窗口：周一至周五 [09:00, 17:00) UTC，节假日除外
_WORK_START = time(9, 0)
_WORK_END = time(17, 0)

# 配对状态
PENDING = "pending"
ACTIVE = "active"
PAUSED = "paused"
SUBMITTED = "submitted"

# 事件类型
ACCEPT = "accept"
PAUSE = "pause"
RESUME = "resume"
SUBMIT = "submit"

# 合法状态转换：(当前状态, 事件类型) -> 下一状态；不在表中的组合即非法状态转换
_TRANSITIONS = {
    (PENDING, ACCEPT): ACTIVE,
    (ACTIVE, PAUSE): PAUSED,
    (PAUSED, RESUME): ACTIVE,
    (ACTIVE, SUBMIT): SUBMITTED,
}


class ReviewClockError(ValueError):
    """请求级非法：重复配对 / 同对同刻事件 / 非法状态转换 / 事件引用未知配对。"""


@dataclass(frozen=True)
class PairSpec:
    """一个「稿件—评审人」配对及其工作小时预算（正整数）。"""

    manuscript_id: str
    reviewer_id: str
    budget_work_hours: int


@dataclass(frozen=True)
class ClockEvent:
    """一条 UTC 事件（接受 / 暂停 / 恢复 / 提交）。"""

    type: str
    at: datetime  # 任意时区的感知时刻（或按 UTC 解释的朴素时刻），内部统一归一到 UTC


@dataclass(frozen=True)
class PairReport:
    """查询时刻单个配对的状态与工时报告。"""

    manuscript_id: str
    reviewer_id: str
    status: str
    budget_work_hours: int
    used_work_seconds: int
    remaining_work_seconds: int
    # 预计截止 UTC 时刻；仅 active 状态非空
    deadline_at: datetime | None
    overdue: bool


def as_utc(dt: datetime) -> datetime:
    """归一到 UTC；无时区信息的时刻按 UTC 解释。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_work_day(d: date, holidays: frozenset[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def work_time_between(
    start: datetime, end: datetime, holidays: frozenset[date]
) -> timedelta:
    """[start, end) 内落入工作窗口（周一至周五 09:00—17:00 UTC 且非节假日）的时长。"""
    if end <= start:
        return timedelta(0)
    total = timedelta(0)
    day = start.date()
    last = end.date()
    one_day = timedelta(days=1)
    while day <= last:
        if _is_work_day(day, holidays):
            ws = datetime.combine(day, _WORK_START, tzinfo=timezone.utc)
            we = datetime.combine(day, _WORK_END, tzinfo=timezone.utc)
            lo = max(start, ws)
            hi = min(end, we)
            if hi > lo:
                total += hi - lo
        day += one_day
    return total


def add_work_time(
    start: datetime, needed: timedelta, holidays: frozenset[date]
) -> datetime:
    """从 start 起累计 needed 工作时长后的 UTC 时刻（跨下班、周末与节假日顺延）。"""
    if needed <= timedelta(0):
        return start
    day = start.date()
    one_day = timedelta(days=1)
    first = True
    while True:
        if _is_work_day(day, holidays):
            ws = datetime.combine(day, _WORK_START, tzinfo=timezone.utc)
            we = datetime.combine(day, _WORK_END, tzinfo=timezone.utc)
            lo = max(start, ws) if first else ws
            if lo < we:
                avail = we - lo
                if needed <= avail:
                    return lo + needed
                needed -= avail
        first = False
        day += one_day


def _whole_seconds(td: timedelta) -> int:
    """非负时长取整秒（舍弃不足 1 秒的零头）。"""
    return td.days * 86400 + td.seconds


def _report_at(
    spec: PairSpec,
    evs: list[ClockEvent],
    holidays: frozenset[date],
    query_at: datetime,
) -> PairReport:
    """按查询时刻重放（仅 at <= query_at 的事件），计算单个配对的状态与工时报告。

    evs 已按时刻升序排序且通过全量合法性校验，故其任意前缀同样是合法序列。
    """
    budget = timedelta(hours=spec.budget_work_hours)
    state = PENDING
    segments: list[tuple[datetime, datetime]] = []  # 已闭合的计时段
    seg_start: datetime | None = None
    for ev in evs:
        if ev.at > query_at:
            break  # 查询时刻之后的事件不影响查询时刻状态（合法性已另行校验）
        if ev.type in (PAUSE, SUBMIT):
            segments.append((seg_start, ev.at))  # type: ignore[arg-type]
            seg_start = None
        else:  # ACCEPT / RESUME：开启新的计时段
            seg_start = ev.at
        state = _TRANSITIONS[(state, ev.type)]
    if state == ACTIVE:
        # 未闭合的计时段累计到查询时刻
        segments.append((seg_start, query_at))  # type: ignore[arg-type]

    used = timedelta(0)
    for s, e in segments:
        used += work_time_between(s, e, holidays)

    remaining = budget - used
    if remaining < timedelta(0):
        remaining = timedelta(0)
    overdue = used > budget

    deadline: datetime | None = None
    if state == ACTIVE:
        # 沿计时段顺序消耗预算：在某段内耗尽则截止时刻落在该段内（可能在过去），
        # 全部耗尽仍有剩余则从查询时刻起沿工作窗口向后顺延
        rem = budget
        for s, e in segments:
            w = work_time_between(s, e, holidays)
            if w >= rem:
                deadline = add_work_time(s, rem, holidays)
                break
            rem -= w
        else:
            deadline = add_work_time(query_at, rem, holidays)

    return PairReport(
        manuscript_id=spec.manuscript_id,
        reviewer_id=spec.reviewer_id,
        status=state,
        budget_work_hours=spec.budget_work_hours,
        used_work_seconds=_whole_seconds(used),
        remaining_work_seconds=_whole_seconds(remaining),
        deadline_at=deadline,
        overdue=overdue,
    )


def compute_review_clock(
    pairs: list[PairSpec],
    events: list[tuple[tuple[str, str], ClockEvent]],
    holidays: frozenset[date],
    query_at: datetime,
) -> tuple[PairReport, ...]:
    """重放事件流并返回查询时刻各配对报告（按稿件、评审人 ID 排序）。

    pairs：配对及预算列表；events：((稿件 ID, 评审人 ID), 事件) 列表（顺序任意）；
    holidays：节假日日期集合；query_at：查询时刻。
    请求非法时抛 ReviewClockError（重复配对 / 同对同刻事件 / 非法状态转换 /
    事件引用未声明的配对）。
    """
    holidays = frozenset(holidays)
    query_at = as_utc(query_at)

    # 重复配对校验（按声明顺序报告首个重复）
    seen: set[tuple[str, str]] = set()
    for p in pairs:
        key = (p.manuscript_id, p.reviewer_id)
        if key in seen:
            raise ReviewClockError(
                f"重复配对：稿件 {p.manuscript_id} 与评审人 {p.reviewer_id}"
            )
        seen.add(key)

    # 事件归组并归一到 UTC；引用未声明配对的事件即非法
    by_pair: dict[tuple[str, str], list[ClockEvent]] = {
        (p.manuscript_id, p.reviewer_id): [] for p in pairs
    }
    for (mid, rid), ev in events:
        key = (mid, rid)
        if key not in by_pair:
            raise ReviewClockError(
                f"事件引用了未声明的配对：稿件 {mid} / 评审人 {rid}"
            )
        by_pair[key].append(ClockEvent(ev.type, as_utc(ev.at)))

    reports: list[PairReport] = []
    for p in sorted(pairs, key=lambda p: (p.manuscript_id, p.reviewer_id)):
        key = (p.manuscript_id, p.reviewer_id)
        evs = sorted(by_pair[key], key=lambda e: e.at)
        # 同对同刻事件：排序后相邻相等即存在（感知时刻按瞬间比较，
        # 不同时区表示的同一瞬间同样判重）
        for a, b in zip(evs, evs[1:]):
            if a.at == b.at:
                raise ReviewClockError(
                    f"配对 ({key[0]}, {key[1]}) 在同一时刻 "
                    f"{a.at.isoformat()} 存在多个事件"
                )
        # 全量事件的状态机合法性校验（含查询时刻之后的事件）
        state = PENDING
        for ev in evs:
            nxt = _TRANSITIONS.get((state, ev.type))
            if nxt is None:
                raise ReviewClockError(
                    f"配对 ({key[0]}, {key[1]}) 在状态 {state} 下不允许事件 {ev.type}"
                )
            state = nxt
        reports.append(_report_at(p, evs, holidays, query_at))
    return tuple(reports)
