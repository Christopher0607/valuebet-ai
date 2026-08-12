"""API key 不能出现在给用户看的任何地方 —— 回归测试。

真事故：requests 的 raise_for_status() 抛的异常消息里带着**完整请求 URL**，
查询串里就是 apiKey。这条异常一路被 run_full_update 收进 failed_comps →
存进 UpdateLog.detail → /api/status 原样回给前端 → **显示在页面横幅上**。
线上真的出现过：

    401 Client Error: Unauthorized for url:
    https://api.the-odds-api.com/v4/sports/soccer_usa_mls/scores/?apiKey=1f888a5c0d6

key 被截断只是因为 detail 那里做了 [:120]，不是有意保护——换个短一点的
赛事名就会把整把 key 打出来。任何看得到这个页面的人都能拿走它。

这个测试盯三件事：
  1. _redact 能抹掉各种写法的 key
  2. 三个 fetch_* 在 401/429/500 时抛出的异常都不含 key
  3. **源码级**：odds_api 里不许再出现裸的 requests.get / raise_for_status，
     必须走 _get —— 以后新增接口会自动继承脱敏，不用指望作者记得
"""
import sys
import os
import types
import ast

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from app import odds_api                                             # noqa: E402

KEY = "1f888a5c0d62746498c3f7eccf7e4d1b"


def _stub(status, body=None):
    class R:
        status_code = status
        headers = {}
        url = f"https://api.the-odds-api.com/v4/sports/x/odds/?apiKey={KEY}&regions=eu"
        def raise_for_status(self):
            if status >= 400:
                raise Exception(f"{status} Client Error: Bad for url: {self.url}")
        def json(self):
            return body if body is not None else []
    odds_api.requests = types.SimpleNamespace(get=lambda *a, **k: R())


def test_redact_forms():
    cases = [
        f"...?apiKey={KEY}",
        f"...?apiKey={KEY}&regions=eu",
        f"...?api_key={KEY}&x=1",
        f'"apikey={KEY}"',
    ]
    for c in cases:
        out = odds_api._redact(c)
        assert KEY not in out, (c, out)
        assert "***" in out, (c, out)
    assert odds_api._redact("") == ""
    assert odds_api._redact(None) == ""
    print("✅ _redact：apiKey / api_key / apikey 各种写法都抹掉，空值不炸")


def test_fetchers_never_leak():
    odds_api.ODDS_API_KEY = KEY
    calls = [
        ("fetch_upcoming_events", lambda: odds_api.fetch_upcoming_events("mls")),
        ("fetch_h2h_odds",        lambda: odds_api.fetch_h2h_odds("epl")),
        ("fetch_recent_scores",   lambda: odds_api.fetch_recent_scores("mls")),
    ]
    for status in (401, 429, 500):
        _stub(status)
        for name, fn in calls:
            try:
                fn()
                raise AssertionError(f"{name} 在 {status} 时没有抛异常")
            except AssertionError:
                raise
            except Exception as e:
                msg = str(e)
                assert KEY not in msg, (name, status, msg)
                assert "apiKey=" not in msg or "***" in msg, (name, status, msg)
        print(f"✅ HTTP {status}：三个 fetch_* 抛出的异常都不含 key")

    # 401/429 还要给出能照着做的话，不是甩原始 HTTP 错误
    _stub(401)
    try:
        odds_api.fetch_h2h_odds("epl")
    except Exception as e:
        assert "环境变量" in str(e), str(e)
        print(f"   401 的文案: {e}")
    _stub(429)
    try:
        odds_api.fetch_h2h_odds("epl")
    except Exception as e:
        assert "配额" in str(e), str(e)
        print(f"   429 的文案: {e}")


def test_no_bare_requests_in_source():
    """源码级：只有 _get 里允许出现 requests.get / raise_for_status。

    加这条是因为 key 泄漏的根因就是"三个 fetch_* 各自 requests.get"。
    收敛到一个入口之后，必须有东西盯着它不要再散开——否则以后新增一个
    接口、忘了走 _get，就又漏一次，而且同样是线上才发现。
    """
    path = os.path.join(HERE, "..", "app", "odds_api.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    offenders = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if fn.name == "_get":
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and n.attr in ("get", "raise_for_status"):
                base = getattr(n.value, "id", None)
                if base == "requests" or n.attr == "raise_for_status":
                    offenders.append(f"{fn.name} 里有 {base or '?'}.{n.attr}")
    assert not offenders, ("这些地方绕开了 _get，key 会从那里漏出去", offenders)
    print("✅ 源码检查：除 _get 外没有裸的 requests.get / raise_for_status")


def test_updatelog_detail_redacted():
    """最后一道：写进 UpdateLog.detail 的字符串必须过 _redact。

    那条 detail 会**原样显示在前端横幅上**，是真正面向用户的出口。
    脱敏放在最后一道，不能只靠上游自觉。
    """
    src = open(os.path.join(HERE, "..", "app", "updater.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    assigns = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if isinstance(t, ast.Attribute) and t.attr == "detail":
                assigns.append(n)
    assert assigns, "没找到任何 log.detail 赋值，检查本身失效了"
    bad = [ast.unparse(a)[:80] for a in assigns
           if "_redact" not in ast.unparse(a)]
    assert not bad, ("这些 log.detail 赋值没过 _redact", bad)
    print(f"✅ {len(assigns)} 处 log.detail 赋值全部过 _redact")


def main():
    test_redact_forms()
    test_fetchers_never_leak()
    test_no_bare_requests_in_source()
    test_updatelog_detail_redacted()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
