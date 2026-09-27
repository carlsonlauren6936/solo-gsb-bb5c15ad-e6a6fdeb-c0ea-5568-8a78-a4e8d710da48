"""POST /review-clock 的端到端测试。

测试使用的日期（均为 UTC）：
    2026-09-21 周一    2026-09-22 周二    2026-09-23 周三
    2026-09-25 周五    2026-09-26 周六    2026-09-27 周日
    2026-09-28 周一    2026-09-29 周二
工作窗口：周一至周五 09:00—17:00（节假日除外），每天 8 小时。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

H = 3600  # 1 工作小时（秒）


def _body(**overrides) -> dict:
    base = {
        "pairs": [{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 8}],
        "events": [],
        "holidays": [],
        "query_at": "2026-09-21T12:00:00Z",
    }
    base.update(overrides)
    return base


def _post(body: dict):
    return client.post("/review-clock", json=body)


def _one(body: dict) -> dict:
    resp = _post(body)
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    return results[0]


# ---------- 基本计时与截止推算 ----------

def test_active_basic_same_day():
    """周一 09:00 接受、预算 8h，周一 12:00 查询：已用 3h，当天 17:00 截止。"""
    r = _one(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-21T09:00:00Z"},
    ]))
    assert r["status"] == "active"
    assert r["budget_work_hours"] == 8
    assert r["used_work_seconds"] == 3 * H
    assert r["remaining_work_seconds"] == 5 * H
    assert r["deadline_at"] == "2026-09-21T17:00:00Z"
    assert r["overdue"] is False


def test_cross_day_deferral():
    """周一 16:00 接受、预算 4h：当天计 1h，跨下班顺延到周二 12:00 截止。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 4}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T16:00:00Z"},
        ],
        query_at="2026-09-21T17:30:00Z",
    ))
    assert r["used_work_seconds"] == 1 * H
    assert r["remaining_work_seconds"] == 3 * H
    assert r["deadline_at"] == "2026-09-22T12:00:00Z"
    assert r["overdue"] is False


def test_weekend_deferral():
    """周五 16:00 接受、预算 4h：周末不计时，下周一 12:00 截止。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 4}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-25T16:00:00Z"},
        ],
        query_at="2026-09-26T12:00:00Z",
    ))
    assert r["used_work_seconds"] == 1 * H
    assert r["deadline_at"] == "2026-09-28T12:00:00Z"
    assert r["overdue"] is False


def test_holiday_deferral():
    """节假日不计时：周五 16:00 接受、预算 4h、周一为节假日，下周二 12:00 截止。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 4}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-25T16:00:00Z"},
        ],
        holidays=["2026-09-28"],
        query_at="2026-09-27T12:00:00Z",
    ))
    assert r["used_work_seconds"] == 1 * H
    assert r["deadline_at"] == "2026-09-29T12:00:00Z"
    assert r["overdue"] is False


def test_accept_on_weekend_starts_next_workday():
    """周六接受：直到下周一 09:00 才开始计时。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 1}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-26T10:00:00Z"},
        ],
        query_at="2026-09-28T09:30:00Z",
    ))
    assert r["used_work_seconds"] == 1800
    assert r["deadline_at"] == "2026-09-28T10:00:00Z"
    assert r["overdue"] is False


def test_budget_exhausted_exactly_at_query():
    """查询时刻恰好耗尽预算：不算逾期（严格大于才逾期），截止时刻即查询时刻。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 3}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
        ],
        query_at="2026-09-21T12:00:00Z",
    ))
    assert r["used_work_seconds"] == 3 * H
    assert r["remaining_work_seconds"] == 0
    assert r["deadline_at"] == "2026-09-21T12:00:00Z"
    assert r["overdue"] is False


# ---------- 暂停 / 恢复 ----------

def test_pause_stops_clock():
    """暂停期间停止计时，暂停时截止时刻为空。"""
    r = _one(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-21T09:00:00Z"},
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "pause",
         "at": "2026-09-21T12:00:00Z"},
    ], query_at="2026-09-23T12:00:00Z"))
    assert r["status"] == "paused"
    assert r["used_work_seconds"] == 3 * H
    assert r["remaining_work_seconds"] == 5 * H
    assert r["deadline_at"] is None
    assert r["overdue"] is False


def test_resume_after_pause():
    """恢复后继续计时：周一计 3h + 周三计 1h，预算 6h 时周三 17:00 截止。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 6}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "pause",
             "at": "2026-09-21T12:00:00Z"},
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "resume",
             "at": "2026-09-23T14:00:00Z"},
        ],
        query_at="2026-09-23T15:00:00Z",
    ))
    assert r["status"] == "active"
    assert r["used_work_seconds"] == 4 * H
    assert r["remaining_work_seconds"] == 2 * H
    assert r["deadline_at"] == "2026-09-23T17:00:00Z"
    assert r["overdue"] is False


def test_paused_pair_can_be_overdue():
    """暂停前已超预算：暂停不改变已用时长，仍判逾期。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 2}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "pause",
             "at": "2026-09-23T09:00:00Z"},
        ],
        query_at="2026-09-25T09:00:00Z",
    ))
    assert r["status"] == "paused"
    assert r["used_work_seconds"] == 16 * H
    assert r["remaining_work_seconds"] == 0
    assert r["deadline_at"] is None
    assert r["overdue"] is True


# ---------- 提交 ----------

def test_submit_on_time():
    """按时提交：提交后状态为 submitted，截止时刻为空，已用时长冻结在提交时刻。"""
    r = _one(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-21T09:00:00Z"},
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "submit",
         "at": "2026-09-21T16:00:00Z"},
    ], query_at="2026-09-25T12:00:00Z"))
    assert r["status"] == "submitted"
    assert r["used_work_seconds"] == 7 * H
    assert r["remaining_work_seconds"] == 1 * H
    assert r["deadline_at"] is None
    assert r["overdue"] is False


def test_submit_overdue_judged_at_submit_time():
    """提交时刻已超预算即逾期（与查询时刻无关）。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 4}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "submit",
             "at": "2026-09-22T10:00:00Z"},
        ],
        query_at="2026-09-25T12:00:00Z",
    ))
    assert r["status"] == "submitted"
    assert r["used_work_seconds"] == 9 * H
    assert r["remaining_work_seconds"] == 0
    assert r["deadline_at"] is None
    assert r["overdue"] is True


def test_submit_with_pause_segments():
    """暂停段不计入提交时的已用时长。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 8}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "pause",
             "at": "2026-09-21T12:00:00Z"},
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "resume",
             "at": "2026-09-22T09:00:00Z"},
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "submit",
             "at": "2026-09-22T11:00:00Z"},
        ],
        query_at="2026-09-23T09:00:00Z",
    ))
    assert r["status"] == "submitted"
    assert r["used_work_seconds"] == 5 * H  # 周一 3h + 周二 2h
    assert r["overdue"] is False


# ---------- 逾期（计时中） ----------

def test_active_overdue_deadline_in_past():
    """计时中已超预算：逾期，剩余为 0，截止时刻为过去某刻。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 2}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
        ],
        query_at="2026-09-22T12:00:00Z",
    ))
    assert r["status"] == "active"
    assert r["used_work_seconds"] == 11 * H
    assert r["remaining_work_seconds"] == 0
    assert r["deadline_at"] == "2026-09-21T11:00:00Z"
    assert r["overdue"] is True


# ---------- 未接受（pending） ----------

def test_pending_without_events():
    """无任何事件的配对：pending，已用 0，截止为空，不逾期。"""
    r = _one(_body())
    assert r["status"] == "pending"
    assert r["used_work_seconds"] == 0
    assert r["remaining_work_seconds"] == 8 * H
    assert r["deadline_at"] is None
    assert r["overdue"] is False


def test_accept_after_query_is_pending():
    """查询时刻之后的事件不影响查询时刻状态（但仍参与合法性校验）。"""
    r = _one(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-22T09:00:00Z"},
    ], query_at="2026-09-21T12:00:00Z"))
    assert r["status"] == "pending"
    assert r["used_work_seconds"] == 0


def test_future_submit_not_applied_at_query():
    """提交事件在查询时刻之后：查询时刻仍为 active。"""
    r = _one(_body(
        pairs=[{"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 10}],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "submit",
             "at": "2026-09-23T09:00:00Z"},
        ],
        query_at="2026-09-22T09:00:00Z",
    ))
    assert r["status"] == "active"
    assert r["used_work_seconds"] == 8 * H
    assert r["deadline_at"] == "2026-09-22T11:00:00Z"


# ---------- 排序与确定性 ----------

def test_results_sorted_by_pair_ids():
    """结果按 (稿件 ID, 评审人 ID) 排序，与输入顺序无关。"""
    body = _body(
        pairs=[
            {"manuscript_id": "m2", "reviewer_id": "r2", "budget_work_hours": 1},
            {"manuscript_id": "m1", "reviewer_id": "r2", "budget_work_hours": 2},
            {"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 3},
        ],
    )
    resp = _post(body)
    assert resp.status_code == 200
    keys = [(r["manuscript_id"], r["reviewer_id"]) for r in resp.json()["results"]]
    assert keys == [("m1", "r1"), ("m1", "r2"), ("m2", "r2")]
    budgets = [r["budget_work_hours"] for r in resp.json()["results"]]
    assert budgets == [3, 2, 1]


def test_input_order_does_not_matter():
    """事件乱序、节假日重复乱序、配对乱序：输出完全一致。"""
    events = [
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-21T09:00:00Z"},
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "pause",
         "at": "2026-09-21T12:00:00Z"},
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "resume",
         "at": "2026-09-22T09:00:00Z"},
        {"manuscript_id": "m2", "reviewer_id": "r2", "type": "accept",
         "at": "2026-09-21T10:00:00Z"},
    ]
    body1 = _body(
        pairs=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 8},
            {"manuscript_id": "m2", "reviewer_id": "r2", "budget_work_hours": 4},
        ],
        events=events,
        holidays=["2026-09-23", "2026-09-24"],
        query_at="2026-09-25T12:00:00Z",
    )
    body2 = _body(
        pairs=[
            {"manuscript_id": "m2", "reviewer_id": "r2", "budget_work_hours": 4},
            {"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 8},
        ],
        events=list(reversed(events)),
        holidays=["2026-09-24", "2026-09-23", "2026-09-24"],
        query_at="2026-09-25T12:00:00Z",
    )
    r1, r2 = _post(body1), _post(body2)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()


def test_query_at_echoed_normalized_to_utc():
    """响应中的 query_at 归一到 UTC。"""
    resp = _post(_body(query_at="2026-09-21T14:00:00+02:00"))
    assert resp.status_code == 200
    assert resp.json()["query_at"] == "2026-09-21T12:00:00Z"


def test_naive_datetime_treated_as_utc():
    """无时区信息的时刻按 UTC 解释。"""
    r = _one(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-21T09:00:00"},
    ], query_at="2026-09-21T12:00:00"))
    assert r["used_work_seconds"] == 3 * H


def test_offset_datetime_normalized():
    """带时区偏移的时刻按瞬间归一：+02:00 的 11:00 即 UTC 09:00。"""
    r = _one(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-21T11:00:00+02:00"},
    ]))
    assert r["used_work_seconds"] == 3 * H


# ---------- 422：请求非法 ----------

def test_422_duplicate_pair():
    resp = _post(_body(pairs=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 1},
        {"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 2},
    ]))
    assert resp.status_code == 422


def test_422_event_references_unknown_pair():
    resp = _post(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "rX", "type": "accept",
         "at": "2026-09-21T09:00:00Z"},
    ]))
    assert resp.status_code == 422


def test_422_same_pair_same_instant():
    resp = _post(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-21T09:00:00Z"},
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-21T09:00:00Z"},
    ]))
    assert resp.status_code == 422


def test_422_same_instant_across_timezones():
    """不同时区表示的同一瞬间也算同对同刻。"""
    resp = _post(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-21T09:00:00Z"},
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "pause",
         "at": "2026-09-21T11:00:00+02:00"},
    ]))
    assert resp.status_code == 422


def test_same_instant_different_pairs_ok():
    """不同配对允许同一时刻的事件。"""
    resp = _post(_body(
        pairs=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 1},
            {"manuscript_id": "m2", "reviewer_id": "r2", "budget_work_hours": 1},
        ],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
            {"manuscript_id": "m2", "reviewer_id": "r2", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
        ],
    ))
    assert resp.status_code == 200


def test_422_illegal_transitions():
    """非法状态转换：未接受先暂停 / 计时中恢复 / 重复接受 / 暂停中暂停 /
    暂停中提交 / 提交后再有事件。"""
    bad_sequences = [
        ["pause"],                       # 未接受先暂停
        ["resume"],                      # 未接受先恢复
        ["submit"],                      # 未接受先提交
        ["accept", "resume"],            # 计时中恢复
        ["accept", "accept"],            # 重复接受
        ["accept", "pause", "pause"],    # 暂停中暂停
        ["accept", "pause", "submit"],   # 暂停中提交
        ["accept", "submit", "pause"],   # 提交后再暂停
        ["accept", "submit", "submit"],  # 提交后再提交
    ]
    for i, seq in enumerate(bad_sequences):
        events = [
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": t,
             "at": f"2026-09-21T{9 + i:02d}:00:00Z"}
            for i, t in enumerate(seq)
        ]
        resp = _post(_body(events=events))
        assert resp.status_code == 422, f"序列 {seq} 应被拒绝: {resp.text}"


def test_422_illegal_transition_after_query():
    """查询时刻之后的非法转换同样拒绝（事件日志整体必须自洽）。"""
    resp = _post(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
         "at": "2026-09-21T09:00:00Z"},
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "pause",
         "at": "2026-09-22T09:00:00Z"},
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "pause",
         "at": "2026-09-23T09:00:00Z"},
    ], query_at="2026-09-21T12:00:00Z"))
    assert resp.status_code == 422


def test_422_invalid_event_type_and_budget():
    """事件类型非法 / 预算非正整数：请求校验拒绝。"""
    resp = _post(_body(events=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "type": "cancel",
         "at": "2026-09-21T09:00:00Z"},
    ]))
    assert resp.status_code == 422
    resp = _post(_body(pairs=[
        {"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 0},
    ]))
    assert resp.status_code == 422


# ---------- 综合场景 ----------

def test_multi_pair_mixed_states():
    """多配对混合状态：active / paused / submitted / pending 同报。"""
    body = _body(
        pairs=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "budget_work_hours": 8},
            {"manuscript_id": "m1", "reviewer_id": "r2", "budget_work_hours": 4},
            {"manuscript_id": "m2", "reviewer_id": "r1", "budget_work_hours": 16},
            {"manuscript_id": "m2", "reviewer_id": "r2", "budget_work_hours": 2},
        ],
        events=[
            {"manuscript_id": "m1", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
            {"manuscript_id": "m1", "reviewer_id": "r2", "type": "accept",
             "at": "2026-09-21T09:00:00Z"},
            {"manuscript_id": "m1", "reviewer_id": "r2", "type": "pause",
             "at": "2026-09-21T11:00:00Z"},
            {"manuscript_id": "m2", "reviewer_id": "r1", "type": "accept",
             "at": "2026-09-21T10:00:00Z"},
            {"manuscript_id": "m2", "reviewer_id": "r1", "type": "submit",
             "at": "2026-09-22T10:00:00Z"},
        ],
        query_at="2026-09-22T12:00:00Z",
    )
    resp = _post(body)
    assert resp.status_code == 200
    results = {(r["manuscript_id"], r["reviewer_id"]): r for r in resp.json()["results"]}

    r = results[("m1", "r1")]
    assert r["status"] == "active" and r["used_work_seconds"] == 11 * H
    assert r["remaining_work_seconds"] == 0
    assert r["deadline_at"] == "2026-09-21T17:00:00Z" and r["overdue"] is True

    r = results[("m1", "r2")]
    assert r["status"] == "paused" and r["used_work_seconds"] == 2 * H
    assert r["remaining_work_seconds"] == 2 * H
    assert r["deadline_at"] is None and r["overdue"] is False

    r = results[("m2", "r1")]
    assert r["status"] == "submitted" and r["used_work_seconds"] == 8 * H
    assert r["remaining_work_seconds"] == 8 * H
    assert r["deadline_at"] is None and r["overdue"] is False

    r = results[("m2", "r2")]
    assert r["status"] == "pending" and r["used_work_seconds"] == 0
    assert r["remaining_work_seconds"] == 2 * H
    assert r["deadline_at"] is None and r["overdue"] is False
