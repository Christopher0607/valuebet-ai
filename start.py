#!/usr/bin/env python3
"""
一键启动 —— 双击这个文件（或跑 python3 start.py）就行。

做的事：
  1. 检查依赖，缺了就装
  2. 如果前端还没打包过，自动打包一次
  3. 启动后端（同时托管前端，只有一个进程、一个端口）
  4. 自动打开浏览器

关掉这个窗口就等于停掉服务。12小时自动更新只在这个进程活着的时候有效。
"""
import os
import sys
import time
import subprocess
import webbrowser
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend")
DIST = os.path.join(FRONTEND, "dist")
PORT = 8000
URL = f"http://127.0.0.1:{PORT}"


def lan_ip():
    """本机在局域网里的地址，手机要用这个连。

    不做真的联网，只是建一个 UDP socket 让系统选出默认出口网卡，
    再问它本地地址是什么。比 gethostbyname(hostname) 可靠——后者在很多
    机器上会返回 127.0.0.1，那样手机就连不上。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))       # 不发数据，只为了确定出口网卡
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def run(cmd, cwd=None, check=True):
    print(f"  $ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=cwd)
    if check and r.returncode != 0:
        raise SystemExit(f"命令失败: {' '.join(cmd)}")
    return r


def ensure_backend_deps():
    try:
        import fastapi, uvicorn, sqlalchemy, apscheduler  # noqa: F401
        print("✅ 后端依赖已就绪")
    except ImportError:
        print("📦 首次运行，安装后端依赖（要一两分钟）...")
        run([sys.executable, "-m", "pip", "install", "-r",
             os.path.join(BACKEND, "requirements.txt"), "--quiet"], check=False)
        print("✅ 后端依赖安装完成")


def ensure_frontend_built():
    if os.path.isdir(DIST) and os.path.exists(os.path.join(DIST, "index.html")):
        print("✅ 前端已打包")
        return
    print("📦 首次运行，打包前端（要两三分钟）...")
    npm = "npm.cmd" if os.name == "nt" else "npm"
    if not os.path.isdir(os.path.join(FRONTEND, "node_modules")):
        run([npm, "install"], cwd=FRONTEND, check=False)
    run([npm, "run", "build"], cwd=FRONTEND, check=False)
    if not os.path.exists(os.path.join(DIST, "index.html")):
        print("⚠️  前端打包失败。后端仍会启动，但网页打不开。")
        print("   可以手动进 frontend 目录跑 npm install && npm run build 看具体报错。")
    else:
        print("✅ 前端打包完成")


def open_browser_when_ready():
    """等服务真的起来了再开浏览器，不然会开出一个连接失败的页面。"""
    import urllib.request
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{URL}/api/status", timeout=1)
            webbrowser.open(URL)
            return
        except Exception:
            time.sleep(1)
    print("⚠️  服务启动超时，请手动打开", URL)


def main():
    print("=" * 50)
    print("  ValueBet 启动中")
    print("=" * 50)
    ensure_backend_deps()
    ensure_frontend_built()

    ip = lan_ip()
    print(f"\n🚀 启动服务")
    print(f"   本机  → {URL}")
    if ip:
        print(f"   手机  → http://{ip}:{PORT}    （手机要连同一个 Wi-Fi）")
    else:
        print("   手机  → 没能识别本机局域网地址，可以自己查（ipconfig / ifconfig）")
    print("   （关掉这个窗口就停止服务；12小时自动更新只在窗口开着时有效）\n")

    threading.Thread(target=open_browser_when_ready, daemon=True).start()

    sys.path.insert(0, BACKEND)
    os.chdir(BACKEND)          # 数据库文件跟着后端目录走
    import uvicorn
    from app.main import app
    # 绑 0.0.0.0 而不是 127.0.0.1，否则只有本机连得上，手机连不进来。
    # 这是局域网内可访问，不是公网——但也意味着同一个 Wi-Fi 下的其他设备
    # 都能打开这个页面，页面里有你的实盘记录。在不信任的网络（咖啡厅、
    # 公共 Wi-Fi）下不要这样跑。
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止。")
    except Exception as e:
        print(f"\n❌ 启动失败: {e}")
        input("按回车键关闭...")
