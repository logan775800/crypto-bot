"""发给用户的中文里不许有 Markdown 斜体。

中文没有真正的斜体字形，Telegram 是把方块字**强行倾斜**画出来的，读起来歪。
英文斜体的排版传统在中文里不成立。这条测试防止以后有人顺手又写回去。

（顺带记一个教训：批量去斜体的第一版正则把 `notify_admin(...中文...__name__)`
里两个不相干的下划线当成一对标记，直接改坏了代码。所以下面的检测也用
同一条严格规则——斜体标记的下划线两侧不可能紧挨着标识符字符。）
"""
import glob
import io
import re

import pytest

# 与批量脚本同一条规则：前后不挨标识符字符，中间含中日韩字符
ITALIC_CJK = re.compile(
    r"(?<![A-Za-z0-9_])_([^_\n`]*[\u4e00-\u9fff][^_\n`]*)_(?![A-Za-z0-9_])")
# 内容是纯变量插值的斜体：f"　_{desc}_"
ITALIC_VAR = re.compile(r"""(?:(?<=["'])|(?<=\\n)|(?<=\s)|(?<=　))_\{[^}]+\}_""")

FILES = sorted(glob.glob("handlers/*.py")) + ["bot.py"]


def _hits(pattern):
    out = []
    for path in FILES:
        try:
            src = io.open(path, encoding="utf-8").read()
        except OSError:
            continue
        for i, line in enumerate(src.split("\n"), 1):
            if line.lstrip().startswith("#"):
                continue                      # 注释里写什么都行
            for m in pattern.finditer(line):
                out.append(f"{path}:{i}  {m.group(0)[:60]}")
    return out


def test_no_chinese_italics_in_user_facing_text():
    hits = _hits(ITALIC_CJK)
    assert not hits, "中文斜体（歪字）：\n" + "\n".join(hits)


def test_no_italics_around_interpolated_values():
    hits = _hits(ITALIC_VAR)
    assert not hits, "变量插值外面套了斜体：\n" + "\n".join(hits)


def test_tuple_unpacking_is_not_mistaken_for_italics():
    """`_, ok, _` 这种元组解包不能被当成斜体误改——第一版脚本就栽在这类误配上。"""
    assert not ITALIC_CJK.search("for name, ok, _ in self.calls")
    assert not ITALIC_CJK.search("_, _ = foo()")


def test_identifier_underscores_are_safe():
    """跨中文的两个标识符下划线绝不能被当成一对标记。"""
    for code in ('await notify_admin(ctx, f"机器人异常{type(e).__name__}")',
                 'tag = "实时" if d <= FRESH_OK else ("偏慢" if d < FRESH_WARN else "过期")',
                 'data["risk_profile"]["单笔风险"] = p["max_risk_pct"]'):
        assert not ITALIC_CJK.search(code), f"会误伤：{code}"
