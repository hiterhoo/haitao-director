# 平台自更新：页面上传更新包（.tar.gz）→ 备份旧代码 → 覆盖新代码（data/ 不动）
# → 自动重启 → 健康检查失败由 update_restart.sh 自动回滚。
# 更新口令在 data/update_token.txt（首次启动自动生成，也可用环境变量 UPDATE_TOKEN 覆盖）。
# 更新包格式：tar.gz，里面是 visual-platform/ 目录（或直接放文件），必须含 app.py 和 config.py。
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request

from flask import jsonify, request

# 更新时绝不覆盖/删除的顶层条目（用户数据、ComfyUI 同步目录、缓存、日志）
PRESERVE = {"data", "input", "output", "__pycache__"}
MAX_PACKAGE_BYTES = 50 * 1024 * 1024


def _is_preserved(name):
    return name in PRESERVE or name.endswith(".log")


def _token_path(base):
    return os.path.join(base, "data", "update_token.txt")


def get_token(base):
    env = os.environ.get("UPDATE_TOKEN", "").strip()
    if env:
        return env
    path = _token_path(base)
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(18)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(token)
    return token


def _read_version(config_path):
    try:
        with open(config_path, encoding="utf-8") as f:
            m = re.search(r'^\s*VERSION\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        return m.group(1) if m else "?"
    except OSError:
        return "?"


def _check_tar(tf):
    """拒绝绝对路径和 .. 穿越；返回成员列表，非法返回 None。"""
    members = tf.getmembers()
    for m in members:
        name = m.name.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            return None
        if m.issym() or m.islnk():
            return None
    return members


def _copy_entry(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, symlinks=False)
    else:
        shutil.copy2(src, dst)


def _remove_entry(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.exists(path) or os.path.islink(path):
        os.remove(path)


def _ver_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _fetch_json(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def check_remote(config):
    """拉远程更新清单，和本地版本号比大小。返回 dict（ok=False 时带 error）。"""
    url = (getattr(config, "UPDATE_MANIFEST_URL", "") or "").strip()
    current = str(getattr(config, "VERSION", "?"))
    if not url:
        return {"ok": False, "error": "未配置远程更新地址（config.UPDATE_MANIFEST_URL）", "current": current}
    try:
        m = _fetch_json(url)
    except Exception as e:
        return {"ok": False, "error": "拉取更新清单失败：%s" % e, "current": current}
    latest = str(m.get("version", "")).strip()
    ci, li = _ver_int(current), _ver_int(latest)
    has = bool(li is not None and ci is not None and li > ci)
    return {
        "ok": True,
        "current": current,
        "latest": latest,
        "has_update": has,
        "notes": str(m.get("notes") or ""),
        "package_url": str(m.get("package_url") or ""),
        "sha256": str(m.get("sha256") or "").strip().lower(),
    }


def _download(url, dest):
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        total = 0
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("更新包超过 50MB，拒绝")
            f.write(chunk)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def _log(base, msg):
    try:
        os.makedirs(os.path.join(base, "data"), exist_ok=True)
        with open(os.path.join(base, "data", "update.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def _apply_package(base, parent, config, pkg_path):
    """把更新包装上：解压校验 → 备份旧版 → 覆盖（保留 data 等）→ 脱离进程重启（失败回滚）。
    成功返回 (old_version, new_version)，校验不过抛 ValueError。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    staging = os.path.join(parent, ".update-staging-" + ts)
    try:
        if os.path.getsize(pkg_path) > MAX_PACKAGE_BYTES:
            raise ValueError("更新包超过 50MB，拒绝")
        os.makedirs(staging, exist_ok=True)
        with tarfile.open(pkg_path, "r:gz") as tf:
            members = _check_tar(tf)
            if members is None:
                raise ValueError("更新包里有非法路径，拒绝")
            tf.extractall(staging, members=members)
        src = os.path.join(staging, "visual-platform")
        if not os.path.isdir(src):
            src = staging
        if not (os.path.isfile(os.path.join(src, "app.py"))
                and os.path.isfile(os.path.join(src, "config.py"))):
            raise ValueError("更新包里缺 app.py 或 config.py，不是平台源码包")
        new_version = _read_version(os.path.join(src, "config.py"))
        old_version = getattr(config, "VERSION", "?")

        # 备份当前代码（不含 data/input/output/日志）
        backup = os.path.join(parent, "visual-platform-bak-" + ts)
        os.makedirs(backup, exist_ok=True)
        for name in os.listdir(base):
            if _is_preserved(name):
                continue
            _copy_entry(os.path.join(base, name), os.path.join(backup, name))

        # 覆盖新代码（data 等保留项不动），并清掉新版已删除的旧文件
        new_names = set()
        for name in os.listdir(src):
            if _is_preserved(name):
                continue
            new_names.add(name)
            dst = os.path.join(base, name)
            _remove_entry(dst)
            _copy_entry(os.path.join(src, name), dst)
        for name in os.listdir(base):
            if _is_preserved(name) or name in new_names:
                continue
            _remove_entry(os.path.join(base, name))

        os.makedirs(os.path.join(base, "data"), exist_ok=True)
        with open(os.path.join(base, "data", "last_update.json"), "w", encoding="utf-8") as f:
            json.dump({
                "from": old_version,
                "to": new_version,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "backup": backup,
            }, f, ensure_ascii=False)

        # 只保留最近 3 份备份，避免占磁盘
        try:
            baks = sorted(d for d in os.listdir(parent) if d.startswith("visual-platform-bak-"))
            for old in baks[:-3]:
                shutil.rmtree(os.path.join(parent, old), ignore_errors=True)
        except OSError:
            pass

        # 脱离当前进程重启（脚本先睡 2 秒让响应发出去），健康检查失败自动回滚
        script = os.path.join(base, "update_restart.sh")
        log_path = os.path.join(base, "data", "update.log")
        with open(log_path, "a", encoding="utf-8") as logf:
            subprocess.Popen(
                ["bash", script, base, str(config.PORT), sys.executable, backup],
                stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                start_new_session=True, env=os.environ.copy(),
            )
        return old_version, new_version
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _queue_idle(ctx):
    jobs = ctx.get("JOBS") or {}
    lock = ctx.get("JOBS_LOCK")
    if lock:
        with lock:
            vals = list(jobs.values())
    else:
        vals = list(jobs.values())
    return not any(j.get("status") in ("queued", "running") for j in vals)


def _remote_apply(base, parent, config, info):
    """按清单下载更新包、校验 sha256、装上。成功返回 (old, new)。"""
    if not info.get("package_url"):
        raise ValueError("更新清单里没有 package_url")
    ts = time.strftime("%Y%m%d-%H%M%S")
    pkg_path = os.path.join(parent, ".update-remote-" + ts + ".tar.gz")
    try:
        _download(info["package_url"], pkg_path)
        if info.get("sha256"):
            got = _sha256_file(pkg_path)
            if got != info["sha256"]:
                raise ValueError("更新包 sha256 校验不通过，拒绝安装")
        return _apply_package(base, parent, config, pkg_path)
    finally:
        try:
            os.remove(pkg_path)
        except OSError:
            pass


def _auto_loop(base, parent, config, ctx):
    time.sleep(30)  # 等平台先起来
    interval = max(1, int(getattr(config, "UPDATE_CHECK_INTERVAL_H", 6) or 6)) * 3600
    while True:
        try:
            info = check_remote(config)
            if info.get("ok") and info.get("has_update"):
                if getattr(config, "AUTO_UPDATE", True) and _queue_idle(ctx):
                    _log(base, "发现新版 v%s（当前 v%s），队列空闲，自动更新" % (info["latest"], info["current"]))
                    _remote_apply(base, parent, config, info)
                    return  # 已触发重启，本进程即将被替换
                else:
                    _log(base, "发现新版 v%s（当前 v%s），等待空闲或手动更新" % (info["latest"], info["current"]))
        except Exception as e:
            _log(base, "自动检查更新出错：%s" % e)
        time.sleep(interval)


def register(app, ctx):
    config = ctx["config"]
    base = config.BASE
    parent = os.path.dirname(base) or "/"
    get_token(base)  # 首次启动就把口令文件生成出来

    @app.route("/api/version")
    def api_version():
        last = None
        try:
            with open(os.path.join(base, "data", "last_update.json"), encoding="utf-8") as f:
                last = json.load(f)
        except (OSError, ValueError):
            pass
        return jsonify({"version": getattr(config, "VERSION", "?"), "last_update": last})

    @app.route("/api/update", methods=["POST"])
    def api_update():
        token = (request.form.get("token") or request.headers.get("X-Update-Token") or "").strip()
        if not token or not hmac.compare_digest(token, get_token(base)):
            return jsonify({"error": "更新口令不对"}), 401
        upload = request.files.get("package")
        if upload is None or not upload.filename:
            return jsonify({"error": "没收到更新包文件"}), 400
        ts = time.strftime("%Y%m%d-%H%M%S")
        pkg_path = os.path.join(parent, ".update-pkg-" + ts + ".tar.gz")
        try:
            upload.save(pkg_path)
            old_version, new_version = _apply_package(base, parent, config, pkg_path)
            return jsonify({
                "ok": True,
                "from": old_version,
                "to": new_version,
                "message": "更新包已装好，平台正在重启，约 15 秒后自动刷新；新版起不来会自动回滚旧版。",
            })
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": "更新失败：" + str(e)}), 500
        finally:
            try:
                os.remove(pkg_path)
            except OSError:
                pass

    @app.route("/api/update/check")
    def api_update_check():
        return jsonify(check_remote(config))

    @app.route("/api/update/remote", methods=["POST"])
    def api_update_remote():
        token = (request.form.get("token") or request.headers.get("X-Update-Token") or "").strip()
        if not token or not hmac.compare_digest(token, get_token(base)):
            return jsonify({"error": "更新口令不对"}), 401
        info = check_remote(config)
        if not info.get("ok"):
            return jsonify({"error": info.get("error") or "检查更新失败"}), 400
        if not info.get("has_update"):
            return jsonify({"ok": True, "message": "已经是最新版 v%s" % info["current"]})
        try:
            old_version, new_version = _remote_apply(base, parent, config, info)
            return jsonify({
                "ok": True,
                "from": old_version,
                "to": new_version,
                "message": "新版已装好，平台正在重启，约 15 秒后自动刷新；新版起不来会自动回滚旧版。",
            })
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": "远程更新失败：" + str(e)}), 500

    # 配置了远程清单地址就启动后台自动检查（发现新版且队列空闲时自动装）
    if (getattr(config, "UPDATE_MANIFEST_URL", "") or "").strip():
        threading.Thread(target=_auto_loop, args=(base, parent, config, ctx), daemon=True).start()
