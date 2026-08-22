"""外部定时触发口 —— 安全边界 + 闸门 + 不阻塞。

为什么要单独一个脚本：/api/cron/update 被放进了 _PUBLIC_PREFIXES，
**绕过了 Supabase JWT 中间件**。也就是说这个项目里所有接口都由中间件
默认拒绝、显式放行，只有这一个是自己管自己的门。写松一点就是把
「让后端跑一轮全量更新」开放给全网——所以这里绝大部分断言都在守这道门，
而不是在测功能。

另一半测的是「这次该不该跑」的闸门：加了保活之后进程能长时间活着，
进程内那个 12 小时的 IntervalTrigger 会真的开始触发，跟外部定时叠加成
一天 4 次，而 DEPLOY.md 估 The Odds API 免费额度（500 次/月）用的是
「每天 2 次」。闸门失效不会报错，只会在月底把配额用光。

跑：cd backend && python3 validation/32_cron_trigger.py
"""
import os
import sys
import time
import tempfile
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

SECRET = "test-cron-secret-do-not-use-in-prod"
DB = os.path.join(tempfile.mkdtemp(), "cron.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["CRON_SECRET"] = SECRET

import logging                                                      # noqa: E402
logging.disable(logging.INFO)

from fastapi.testclient import TestClient                           # noqa: E402
from app import main as app_main                                    # noqa: E402
from app import updater                                             # noqa: E402
from app.models import (SessionLocal, engine, Base, Competition,    # noqa: E402
                        Match, Prediction, UpdateLog)

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)

failed = 0


def check(name, cond, extra=""):
    global failed
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}" + (f" —— {extra}" if extra else ""))
        failed += 1


# 更新本身要花时间抓网络，这里换成一个可控的假动作：只关心「有没有被触发」
# 和「跑的时候锁是不是握着的」，不关心它抓了什么。
_ran = []


def fake_update(db):
    _ran.append(dt.datetime.utcnow())
    time.sleep(0.35)                      # 留出窗口，好观察 update_running
    row = UpdateLog(matches_updated=1, predictions_updated=1, bets_resolved=0, status="ok")
    db.add(row)
    db.commit()
    return {"status": "ok"}


def install_fake():
    """替掉 run_full_update，但**保留真实的锁**——并发行为是被测对象之一。"""
    def wrapped(db):
        if not updater._update_lock.acquire(blocking=False):
            return {"status": "skipped"}
        try:
            return fake_update(db)
        finally:
            updater._update_lock.release()
    app_main.run_full_update = wrapped


install_fake()
client = TestClient(app_main.app)


def wait_idle(timeout=6.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not updater._update_lock.locked():
            return True
        time.sleep(0.05)
    return False


def n_logs():
    db = SessionLocal()
    try:
        return db.query(UpdateLog).count()
    finally:
        db.close()


def clear_logs():
    db = SessionLocal()
    try:
        db.query(UpdateLog).delete()
        db.commit()
    finally:
        db.close()


print("外部定时触发口 /api/cron/update\n")

print("【1】这道门（唯一的一道，因为它绕过了 JWT 中间件）")
before = len(_ran)
r = client.post("/api/cron/update")
check("不带 X-Cron-Key → 403", r.status_code == 403, f"实际 {r.status_code}")
r = client.post("/api/cron/update", headers={"X-Cron-Key": "wrong"})
check("key 不对 → 403", r.status_code == 403, f"实际 {r.status_code}")
r = client.post("/api/cron/update", headers={"X-Cron-Key": SECRET[:-1]})
check("key 少一个字符 → 403", r.status_code == 403, f"实际 {r.status_code}")
r = client.post("/api/cron/update", headers={"X-Cron-Key": SECRET + "x"})
check("key 多一个字符 → 403", r.status_code == 403, f"实际 {r.status_code}")
check("以上四次一次都没触发更新", len(_ran) == before,
      f"却触发了 {len(_ran) - before} 次 —— 未授权的人可以让后端跑全量更新")

print("\n【2】没配 CRON_SECRET 时必须禁用，不是放行")
saved = app_main.CRON_SECRET
app_main.CRON_SECRET = ""
before = len(_ran)
for hdr in ({}, {"X-Cron-Key": ""}, {"X-Cron-Key": SECRET}):
    r = client.post("/api/cron/update", headers=hdr)
    check(f"CRON_SECRET 为空时 headers={hdr or '无'} → 503",
          r.status_code == 503, f"实际 {r.status_code}")
check("一次都没触发", len(_ran) == before)
app_main.CRON_SECRET = saved

print("\n【3】key 正确 → 立刻返回 202，更新在后台跑")
clear_logs()
t0 = time.time()
r = client.post("/api/cron/update", headers={"X-Cron-Key": SECRET})
elapsed = time.time() - t0
check("返回 202", r.status_code == 202, f"实际 {r.status_code} {r.text[:120]}")
# 假更新本身 sleep 0.35 秒；接口若是同步跑，这里必然 ≥0.35
check(f"立刻返回、没等更新跑完（耗时 {elapsed * 1000:.0f} ms）", elapsed < 0.3,
      "同步跑的话冷启动 50 秒 + 更新耗时会把调用方的超时撞穿")
check("此时 /api/status 报 update_running=true",
      client.get("/api/status").json()["update_running"] is True)
check("更新真的跑起来了", wait_idle(), "等了 6 秒锁还没放开")
check("跑完后写了一行 update_log", n_logs() == 1, f"实际 {n_logs()} 行")
check("跑完后 update_running=false",
      client.get("/api/status").json()["update_running"] is False)

print("\n【4】闸门：刚跑完就再触发一次，应该跳过")
before = len(_ran)
r = client.post("/api/cron/update", headers={"X-Cron-Key": SECRET})
check("回 200 且 status=skipped",
      r.status_code == 200 and r.json().get("status") == "skipped",
      f"{r.status_code} {r.text[:160]}")
check("没有重复触发更新", len(_ran) == before,
      "闸门没拦住 —— 保活开了之后会跟进程内定时任务叠加，Odds API 配额会被打爆")

print("\n【5】force=true 可以强跑")
before = len(_ran)
r = client.post("/api/cron/update?force=true", headers={"X-Cron-Key": SECRET})
check("回 202", r.status_code == 202, f"实际 {r.status_code}")
wait_idle()
check("确实跑了一次", len(_ran) == before + 1)
check("force 也要过密钥这一关",
      client.post("/api/cron/update?force=true").status_code == 403)

print("\n【6】闸门是按「上次更新有多久」算的，不是按调用次数")
clear_logs()
db = SessionLocal()
db.add(UpdateLog(ran_at=dt.datetime.utcnow() - dt.timedelta(hours=13),
                 matches_updated=0, predictions_updated=0, bets_resolved=0, status="ok"))
db.commit()
db.close()
before = len(_ran)
r = client.post("/api/cron/update", headers={"X-Cron-Key": SECRET})
check("上次更新在 13 小时前 → 该跑", r.status_code == 202, f"实际 {r.status_code}")
wait_idle()
check("确实跑了", len(_ran) == before + 1)

clear_logs()
db = SessionLocal()
db.add(UpdateLog(ran_at=dt.datetime.utcnow() - dt.timedelta(hours=11),
                 matches_updated=0, predictions_updated=0, bets_resolved=0, status="ok"))
db.commit()
db.close()
before = len(_ran)
r = client.post("/api/cron/update", headers={"X-Cron-Key": SECRET})
check("上次更新在 11 小时前 → 跳过",
      r.json().get("status") == "skipped", r.text[:160])
check("确实没跑", len(_ran) == before)

print("\n【7】一轮在跑的时候，再触发不重复起")
clear_logs()
client.post("/api/cron/update?force=true", headers={"X-Cron-Key": SECRET})
time.sleep(0.05)
before = len(_ran)
r = client.post("/api/cron/update?force=true", headers={"X-Cron-Key": SECRET})
check("回 409 already_running",
      r.status_code == 409 and r.json().get("status") == "already_running",
      f"{r.status_code} {r.text[:160]}")
check("没有起第二个线程", len(_ran) == before)
wait_idle()

print("\n【8】「立即更新」按钮：立刻返回，人点的不受闸门限制")
clear_logs()
db = SessionLocal()
db.add(UpdateLog(ran_at=dt.datetime.utcnow(), matches_updated=0,
                 predictions_updated=0, bets_resolved=0, status="ok"))
db.commit()
db.close()
before = len(_ran)
t0 = time.time()
r = client.post("/api/update-now")
elapsed = time.time() - t0
check("回 202", r.status_code == 202, f"实际 {r.status_code} {r.text[:120]}")
check(f"立刻返回（{elapsed * 1000:.0f} ms）", elapsed < 0.3,
      "同步跑会把前端那个 120 秒超时撞穿，用户看到「点了没反应」")
wait_idle()
check("刚刚才更新过也照样跑（人主动点的就是想现在更新）", len(_ran) == before + 1)

print("\n【9】加了公开前缀之后，别的接口不能跟着敞开")
# 这一条是防「为了让 cron 走通，把 _PUBLIC_PREFIXES 写宽了」——
# 比如写成 "/api/" 或者 "/api/cron" 少个斜杠导致 /api/cronfoo 也放行。
from app.main import _PUBLIC_PREFIXES                               # noqa: E402
check("公开前缀只有 health 和 cron 两条",
      set(_PUBLIC_PREFIXES) == {"/api/health", "/api/cron/"},
      str(_PUBLIC_PREFIXES))
check("cron 前缀带结尾斜杠（否则 /api/cronxxx 也会被放行）",
      all(p.endswith("/") or p == "/api/health" for p in _PUBLIC_PREFIXES),
      str(_PUBLIC_PREFIXES))
for path in ("/api/matches", "/api/bets", "/api/settings", "/api/update-now",
             "/api/parlay-bets", "/api/update-log"):
    check(f"{path} 不在公开名单里", not path.startswith(tuple(_PUBLIC_PREFIXES)))

print("\n【10】开着 Supabase 认证时（也就是线上那套配置）的边界")
# 上面那些都跑在 AUTH_ENABLED=False 下（本地没配 Supabase），中间件根本不介入，
# 所以它们**测不到**「cron 被放行、其余接口照旧要令牌」这件事——而那正是这次
# 改动唯一动过的安全边界。认证是模块导入时按环境变量定死的，改不了，
# 只能另起一个进程测。
import subprocess                                                   # noqa: E402
import json as _json                                                # noqa: E402

_probe = r"""
import os, sys, json, tempfile
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mkdtemp() + "/auth.db"
os.environ["CRON_SECRET"] = %(secret)r
# 配上 JWT 密钥 = 云端那套，AUTH_ENABLED 变 True
os.environ["SUPABASE_JWT_SECRET"] = "dummy-jwt-secret-for-this-probe"
sys.path.insert(0, %(backend)r)
import logging; logging.disable(logging.INFO)
from fastapi.testclient import TestClient
from app import main as m
from app.models import Base, engine
Base.metadata.create_all(engine)
assert m.AUTH_ENABLED is True, "这个探针要在认证打开的状态下跑"
# 别真去抓网络
m.run_full_update = lambda db: {"status": "ok"}
c = TestClient(m.app)
out = {
    "auth_enabled": m.AUTH_ENABLED,
    "cron_no_token": c.post("/api/cron/update?force=true",
                            headers={"X-Cron-Key": %(secret)r}).status_code,
    "cron_bad_key": c.post("/api/cron/update",
                           headers={"X-Cron-Key": "nope"}).status_code,
    "matches_no_token": c.get("/api/matches").status_code,
    "update_now_no_token": c.post("/api/update-now").status_code,
    "update_log_no_token": c.get("/api/update-log").status_code,
    "health_no_token": c.get("/api/health").status_code,
}
print("PROBE" + json.dumps(out))
""" % {"secret": SECRET, "backend": os.path.join(HERE, "..")}

_p = subprocess.run([sys.executable, "-c", _probe], capture_output=True, text=True)
_line = next((l for l in _p.stdout.splitlines() if l.startswith("PROBE")), None)
if not _line:
    check("认证探针跑起来了", False, (_p.stderr or _p.stdout)[-400:])
else:
    d = _json.loads(_line[5:])
    check("探针里认证确实是开着的", d["auth_enabled"] is True)
    check("带正确密钥的 cron 不需要用户令牌就能过（GitHub Actions 拿不到令牌）",
          d["cron_no_token"] == 202, f"实际 {d['cron_no_token']}")
    check("密钥错的 cron 仍然 403（公开前缀不等于不设防）",
          d["cron_bad_key"] == 403, f"实际 {d['cron_bad_key']}")
    check("/api/health 照旧公开", d["health_no_token"] == 200, f"实际 {d['health_no_token']}")
    for k, name in (("matches_no_token", "/api/matches"),
                    ("update_now_no_token", "/api/update-now"),
                    ("update_log_no_token", "/api/update-log")):
        check(f"{name} 没带令牌仍然被拦（401/403）", d[k] in (401, 403),
              f"实际 {d[k]} —— 加公开前缀时把别的接口一起放行了")

print("\n" + ("全部通过。" if failed == 0 else f"{failed} 项失败。"))
sys.exit(0 if failed == 0 else 1)
