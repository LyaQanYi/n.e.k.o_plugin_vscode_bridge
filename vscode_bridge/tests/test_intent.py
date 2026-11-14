from __future__ import annotations

from vscode_bridge.bridge.intent import (
    claim_looks_like_file_change,
    looks_like_coding_request,
)


def test_coding_intent_matches_explicit_requests() -> None:
    assert looks_like_coding_request("把 src/App.tsx 的标题改成 X") is True
    assert looks_like_coding_request("帮我写个函数并补测试") is True
    assert looks_like_coding_request("修复这个报错") is True
    assert looks_like_coding_request("看一下 src/App.tsx") is True


def test_coding_intent_ignores_chat_and_questions() -> None:
    assert looks_like_coding_request("你好") is False
    assert looks_like_coding_request("什么是闭包") is False
    assert looks_like_coding_request("不要修改，只是解释一下") is False


def test_claim_detection() -> None:
    assert claim_looks_like_file_change("我已经修改了文件") is True
    assert claim_looks_like_file_change("The file has been updated") is True
    assert claim_looks_like_file_change("我需要先读一下文件") is False
