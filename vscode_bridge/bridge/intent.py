"""Small, dependency-free intent helpers for the chat coding takeover."""

from __future__ import annotations

import re

_EDIT_WORDS = re.compile(
    r"("
    r"修改|编辑|创建|新增|删除|重命名|修复|实现|重构|补测试|补个测试|写文件|写代码|改成|改为|改一下|改下|帮我改|调整|写|补|更新|替换|移动|复制|"
    r"运行|执行|安装|测试|读取|读一下|看一下|看下|检查|排查|整理|注释|格式化|"
    r"fix|edit|create|add|delete|remove|rename|refactor|implement|write|update|replace|"
    r"run|execute|install|test|read|inspect|check|format"
    r")",
    re.IGNORECASE,
)
_CODING_NOUNS = re.compile(
    r"(代码|文件|项目|函数|方法|类|组件|接口|测试|报错|错误|依赖|脚本|配置|"
    r"code|file|project|function|method|class|component|interface|test|error|bug|dependency|script|config)",
    re.IGNORECASE,
)
_FILE_PATH = re.compile(
    r"("
    r"(?:[\w.-]+/)+[\w.-]+|"
    r"[\w.-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|md|mdx|json|jsonc|css|scss|less|html|vue|svelte|"
    r"go|rs|java|kt|swift|c|cc|cpp|h|hpp|cs|rb|php|sh|zsh|yml|yaml|toml|ini|sql|xml|"
    r"dockerfile|makefile|gradle|lock)"
    r")",
    re.IGNORECASE,
)
_NO_CHANGE = re.compile(
    r"(不要|别|无需|不用).{0,8}(改|修改|写|编辑|动)|"
    r"只是(问|想了解|好奇)|只是解释|别动文件|don't (edit|change|modify|write)|"
    r"no (changes|edits)|not (edit|change|modify)",
    re.IGNORECASE,
)
_PURE_QUESTION = re.compile(
    r"^\s*(什么是|为什么|怎么理解|如何理解|解释一下|讲讲|说说|what is|why|how does|explain)",
    re.IGNORECASE,
)
_CLAIM = re.compile(
    r"("
    r"已(经)?(修改|写入|创建|删除|更新|完成|改好|写好|保存|新增|修复|重构|移动|替换)|"
    r"文件.{0,12}(已|完成|更新|修改|写入)|"
    r"(the )?(file|code|change|edit|workspace).{0,20}(has|have|was|were)?\s*(been )?"
    r"(updated|edited|created|written|fixed|saved|completed|modified)|"
    r"i (have )?(updated|edited|created|written|fixed|saved|completed)|"
    r"(changes?|edits?).{0,8}(applied|saved|completed)"
    r")",
    re.IGNORECASE,
)


def looks_like_coding_request(text: str) -> bool:
    text = (text or "").strip()
    if not text or _NO_CHANGE.search(text):
        return False
    if _FILE_PATH.search(text) and _EDIT_WORDS.search(text):
        return True
    if _PURE_QUESTION.match(text) and not _EDIT_WORDS.search(text):
        return False
    if _EDIT_WORDS.search(text) and _CODING_NOUNS.search(text):
        return True
    if _FILE_PATH.search(text) and re.search(r"(看|读|检查|排查|run|test|read|inspect|check)", text, re.IGNORECASE):
        return True
    return False


def claim_looks_like_file_change(text: str) -> bool:
    text = (text or "").strip()
    return bool(text) and bool(_CLAIM.search(text))
