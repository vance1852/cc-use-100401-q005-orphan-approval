"""装备质量服务的领域错误。"""

from __future__ import annotations


class Conflict(Exception):
    """请求与批次当前修订状态或已生效决定冲突，HTTP 层映射为 409。"""


class StaleRevision(Conflict):
    """调用方提供的修订号落后于批次当前修订号，整个请求必须回滚。"""


class ReviewClosed(Conflict):
    """批次审批已终结，不允许再次作出决定。"""


class ReplayConflict(Conflict):
    """重放内容与已生效决定不一致，消息需指明冲突的版本。"""
