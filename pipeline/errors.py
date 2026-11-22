"""错误类型。

定义错误与快照错误分开：一份坏定义被整体拒绝（不产出部分事实），
而一份坏快照只影响该提交，不连累其他独立答卷。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DefinitionError(Exception):
    """定义中的单条校验问题（非法引用、环、非法作用域等）。"""

    code: str
    message: str
    path: str | None = None

    def __str__(self) -> str:
        loc = f" [{self.path}]" if self.path else ""
        return f"{self.code}{loc}: {self.message}"


@dataclass(frozen=True)
class SnapshotError(Exception):
    """提交快照中的单条结构问题。"""

    code: str
    message: str
    path: str | None = None

    def __str__(self) -> str:
        loc = f" [{self.path}]" if self.path else ""
        return f"{self.code}{loc}: {self.message}"


class DefinitionRejected(Exception):
    """整份定义被拒绝；聚合该定义的全部错误，避免只报第一条。"""

    def __init__(self, errors: list[DefinitionError]):
        self.errors = list(errors)
        super().__init__("; ".join(str(e) for e in self.errors))


class SnapshotRejected(Exception):
    """单份提交快照被拒绝；不影响其他提交。"""

    def __init__(self, errors: list[SnapshotError]):
        self.errors = list(errors)
        super().__init__("; ".join(str(e) for e in self.errors))
