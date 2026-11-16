"""llm.py 全路径测试 —— 基线评分 D8 点名的三条网络异常路径（llmHttpError /
llmNetworkError / llmEmptyContent）与 R1 新增 scheme 白名单的回归护栏。

不连真实端点：urlopen 经 pytest monkeypatch 换成内存假实现（错误路径抛 HTTPError/
OSError），覆盖 llm.py 的注册表契约、工厂期校验与异步客户端的每一条显式抛错分支。
"""

import asyncio
import io
import json
import urllib.error
import urllib.request

import pytest

from py_store import llm


def _run(coro):
    return asyncio.run(coro)


def _patch_urlopen(monkeypatch, impl):
    """替换 llm 客户端用到的 urlopen（llm.py 经 ``urllib.request.urlopen`` 取用）"""
    monkeypatch.setattr(urllib.request, "urlopen", impl)


class _Resp:
    """协议：``with urlopen(req, timeout) as resp: resp.read()``"""

    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _client(**kw):
    base = dict(base_url="http://llm.test/v1", model="m1", api_key="sk-test")
    base.update(kw)
    return llm.make_openai_compat(**base)


_OK = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


# ---------- 注册表契约 ----------


def test_register_duplicate_raises():
    llm.register_llm("dup-probe", lambda msgs: "ok")
    try:
        with pytest.raises(ValueError, match="重复注册"):
            llm.register_llm("dup-probe", lambda msgs: "ok")
    finally:
        # 注册表是模块级单例：无论如何清掉探针名，避免污染其他用例
        llm._REGISTRY.pop("dup-probe", None)


def test_get_llm_unregistered_raises():
    with pytest.raises(KeyError, match="未注册"):
        llm.get_llm("no-such-llm-probe")


def test_get_llm_returns_registered_client():
    probe = lambda msgs: "ok"  # noqa: E731 —— 协议假实现（测试注入假 LLM 同走此口）
    llm.register_llm("get-probe", probe)
    try:
        assert llm.get_llm("get-probe") is probe
    finally:
        llm._REGISTRY.pop("get-probe", None)


# ---------- 工厂期校验（R1 scheme 白名单） ----------


@pytest.mark.parametrize("bad", ["file:///etc/llm", "ftp://llm.test/v1", "gopher://x"])
def test_openai_compat_rejects_non_http_scheme(bad):
    with pytest.raises(RuntimeError) as ei:
        _client(base_url=bad)
    assert ei.value.args[0]["code"] == "llmBaseUrlUnsupported"
    assert ei.value.args[0]["base_url"] == bad


def test_openai_compat_accepts_https():
    assert callable(_client(base_url="https://llm.test/v1"))


# ---------- json_mode 契约预检 ----------


def test_json_mode_requires_json_word_in_prompt(monkeypatch):
    # urlopen 不应被触达（预检先于网络）
    def must_not_call(req, timeout=None):
        raise AssertionError("契约预检失败时不应发起网络请求")

    _patch_urlopen(monkeypatch, must_not_call)
    c = _client(json_mode=True)
    with pytest.raises(RuntimeError) as ei:
        _run(c([{"role": "user", "content": "只回答文字"}]))
    assert ei.value.args[0]["code"] == "llmJsonPromptMissing"


def test_json_mode_passes_when_prompt_mentions_json(monkeypatch):
    _patch_urlopen(
        monkeypatch,
        lambda req, timeout=None: _Resp(
            {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        ),
    )
    c = _client(json_mode=True)
    out = _run(c([{"role": "user", "content": '以 JSON 返回 {"ok":1}'}]))
    assert out == "{}"


# ---------- 请求体构造（response_format / reasoning_effort / UA） ----------


def test_request_body_json_mode_and_effort(monkeypatch):
    captured = {}

    def impl(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["headers"] = dict(req.header_items())
        return _Resp(_OK)

    _patch_urlopen(monkeypatch, impl)
    c = _client(json_mode=True, effort="low")
    out = _run(c([{"role": "user", "content": "回答 JSON 结构"}]))
    assert out == "ok"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["reasoning_effort"] == "low"
    assert captured["body"]["model"] == "m1"
    ua = [v for k, v in captured["headers"].items() if k.lower() == "user-agent"]
    assert ua and ua[0] == "py-store-ask/0.1"


def test_request_body_effort_none_omits_fields(monkeypatch):
    captured = {}

    def impl(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(_OK)

    _patch_urlopen(monkeypatch, impl)
    c = _client(json_mode=False, effort=None)
    out = _run(c([{"role": "user", "content": "hi"}]))
    assert out == "ok"
    assert "response_format" not in captured["body"]
    assert "reasoning_effort" not in captured["body"]


# ---------- 三条网络异常路径（基线 D8 点名） ----------


def test_llm_http_error_carries_status_and_body(monkeypatch):
    def impl(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"upstream down")
        )

    _patch_urlopen(monkeypatch, impl)
    c = _client(json_mode=False)
    with pytest.raises(RuntimeError) as ei:
        _run(c([{"role": "user", "content": "hi"}]))
    err = ei.value.args[0]
    assert err["code"] == "llmHttpError"
    assert err["status"] == 502
    assert "upstream down" in err["body"]


def test_llm_network_error(monkeypatch):
    def impl(req, timeout=None):
        raise OSError("RemoteDisconnected: Remote end closed connection")

    _patch_urlopen(monkeypatch, impl)
    c = _client(json_mode=False)
    with pytest.raises(RuntimeError) as ei:
        _run(c([{"role": "user", "content": "hi"}]))
    err = ei.value.args[0]
    assert err["code"] == "llmNetworkError"
    assert "RemoteDisconnected" in err["detail"]


def test_llm_empty_content_carries_finish_reason(monkeypatch):
    # 推理 token 耗尽场景：content 为空、finish_reason=length、usage 有推理 token
    def impl(req, timeout=None):
        return _Resp(
            {
                "choices": [{"message": {"content": None}, "finish_reason": "length"}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 4096}},
            }
        )

    _patch_urlopen(monkeypatch, impl)
    c = _client(json_mode=False, max_tokens=16)
    with pytest.raises(RuntimeError) as ei:
        _run(c([{"role": "user", "content": "hi"}]))
    err = ei.value.args[0]
    assert err["code"] == "llmEmptyContent"
    assert err["finish_reason"] == "length"
    assert err["reasoning_tokens"] == 4096


def test_llm_missing_choices_is_empty_content(monkeypatch):
    _patch_urlopen(monkeypatch, lambda req, timeout=None: _Resp({"choices": []}))
    c = _client(json_mode=False)
    with pytest.raises(RuntimeError) as ei:
        _run(c([{"role": "user", "content": "hi"}]))
    assert ei.value.args[0]["code"] == "llmEmptyContent"
