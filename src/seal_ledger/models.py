"""报告状态机（严格对应领域契约：试算、会签、封账、导出、更正）。

"导出"是已封账报告上的只读活动，不改变状态；"重开"不是状态，而是
一道独立批准闸门，批准后产生一份全新的继任试算稿，原封账报告在继任稿
封账前始终保持已封账。
"""
from __future__ import annotations

from enum import Enum


class ReportStatus(str, Enum):
    TRIAL = "试算"
    SIGNING = "会签"
    SEALED = "已封账"
    SUPERSEDED = "已更正"

    @property
    def is_sealed(self) -> bool:
        return self is ReportStatus.SEALED


# 允许的状态转移：试算 -> 会签 -> 已封账 -> 已更正（由继任版本封账触发）
TRANSITIONS: dict[ReportStatus, frozenset[ReportStatus]] = {
    ReportStatus.TRIAL: frozenset({ReportStatus.SIGNING}),
    ReportStatus.SIGNING: frozenset({ReportStatus.SEALED}),
    ReportStatus.SEALED: frozenset({ReportStatus.SUPERSEDED}),
    ReportStatus.SUPERSEDED: frozenset(),
}

# 必须完成会签的双方
REQUIRED_PARTIES: frozenset[str] = frozenset({"中方", "外方"})


def can_transition(src: ReportStatus, dst: ReportStatus) -> bool:
    return dst in TRANSITIONS.get(src, frozenset())
