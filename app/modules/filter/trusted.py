"""内置可信制作组（TRUSTED）规则生成。"""

from typing import Iterable

# 组名只在这些分隔符之后出现才算制作组位置：`-GROUP`、`[GROUP]`、`【组】`、`A&B` 联合组等。
_GROUP_PREFIX = r"[-@\[【&＆￡★]"
# 正则元字符；组名按字面匹配，转义后同时兼容 Python re 与 Rust 过滤引擎。
_REGEX_META = set("\\.^$|?*+()[]{}")
# 名单为空时使用的永不命中表达式，保证 TRUSTED 不会放行任何资源。
_NEVER_MATCH = r"[^\s\S]"


def _escape(name: str) -> str:
    return "".join(f"\\{char}" if char in _REGEX_META else char for char in name)


def build_trusted_rule(groups: Iterable[str]) -> dict:
    """
    根据可信组名单生成内置 TRUSTED 过滤规则

    只匹配种子标题中位于分隔符之后、且后面不紧跟字母或数字的组名，避免片名单词或副标题中的同名词误判；
    组名按长度倒序排列，让 `CMCTV` 这类长名优先于其前缀 `CMCT` 尝试。
    :param groups: 可信组名，按字面匹配、不区分大小写
    :return: 可直接放入过滤规则集的规则定义
    """
    names = sorted({name for name in groups if name}, key=lambda name: (-len(name), name))
    if not names:
        return {"include": [_NEVER_MATCH], "exclude": [], "match": ["title"]}
    alternation = "|".join(_escape(name) for name in names)
    return {
        "include": [rf"(?<={_GROUP_PREFIX})(?:{alternation})(?![A-Za-z0-9])"],
        "exclude": [],
        "match": ["title"],
    }
