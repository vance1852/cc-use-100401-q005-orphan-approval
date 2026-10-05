"""装备质量服务层可观察错误。"""


class Conflict(RuntimeError):
    """请求与既有决定或当前版本冲突（重放内容不一致、版本落后等）。"""


class InvalidState(RuntimeError):
    """目标批次当前状态不允许所请求的操作。"""
