"""CS2 Config Saver: сохранение и восстановление конфигов Counter-Strike 2 (бинды, настройки, autoexec).

Интерфейс открывается в отдельном окне браузера (Edge/Chrome в режиме приложения, иначе обычная вкладка).
Сервер слушает только 127.0.0.1, каждый запрос проверяется по случайному токену.
Запуск: python cs2_config_saver.py   (нужен только Python 3.9+, без внешних зависимостей)
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import cs2cfg_core as core

APP = "CS2 Config Saver"
DATA_DIR = Path.home() / "CS2ConfigSaver"
SETTINGS = DATA_DIR / "settings.json"
LOG = DATA_DIR / "error.log"
LAUNCH_OPTION = "+exec autoexec.cfg"
# Браузер замедляет таймеры свёрнутых окон до раза в минуту, поэтому запас большой.
HEARTBEAT_TIMEOUT = 180
MAX_UPLOAD = core.MAX_TOTAL + 1_000_000


def log_error(text: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{text}")
    except OSError:
        pass


def resource(name: str) -> Path:
    """Файл рядом со скриптом или внутри собранного PyInstaller .exe."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


class Backend:
    """Вся логика интерфейса. Методы api_* принимают dict из JSON и возвращают dict."""

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()  # сервер многопоточный, а запись файлов должна идти по одной
        self.store = core.ProfileStore(DATA_DIR / "profiles")
        self.settings = self._load_settings()
        self.steam: Path | None = None
        self.game_cfg: Path | None = None
        self.accounts: list[core.Account] = []
        self.game_running: bool | None = False
        self.steam_running: bool | None = False
        self.last_seen = time.time()
        self.refresh()
        threading.Thread(target=self._poll, daemon=True).start()

    # ---------------------------------------------------------- настройки
    def _load_settings(self) -> dict:
        try:
            s = json.loads(SETTINGS.read_text(encoding="utf-8"))
            return s if isinstance(s, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        try:
            core.write_text_atomic(SETTINGS, json.dumps(self.settings, ensure_ascii=False))
        except OSError:
            pass

    def refresh(self):
        steam = self.settings.get("steam")
        self.steam = core.find_steam_root(steam if isinstance(steam, str) else None)
        self.game_cfg = core.find_game_cfg(self.steam) if self.steam else None
        self.accounts = core.find_accounts(self.steam) if self.steam else []

    def _poll(self):
        """Проверка процессов в фоне: интерфейс получает готовый результат без задержки."""
        while True:
            self.game_running = core.is_running("cs2.exe")
            self.steam_running = core.is_running("steam.exe", "steam")
            time.sleep(4)

    # ---------------------------------------------------------- помощники
    def account(self, aid) -> core.Account:
        for a in self.accounts:
            if a.account_id == str(aid):
                return a
        raise core.ConfigError("Аккаунт не найден. Нажмите «Обновить».")

    def _label(self, aid: str) -> str:
        return next((a.label for a in self.accounts if a.account_id == aid), aid)

    def _profile_json(self, p: core.Profile) -> dict:
        return {"name": p.name, "created": p.created, "note": p.note, "account_id": p.account_id,
                "account_label": self._label(p.account_id) if p.account_id else "", "files": p.files,
                "parts": p.parts, "auto": p.is_auto, "imported": p.imported}

    @staticmethod
    def _parts(d: dict, allowed=("binds", "settings", "video", "game")) -> tuple[str, ...]:
        raw = d.get("parts") or []
        if not isinstance(raw, list):
            raise core.ConfigError("Неверный список частей.")
        parts = tuple(p for p in allowed if p in raw)
        if not parts:
            raise core.ConfigError("Отметьте хотя бы одну часть.")
        return parts

    def _source_dir(self, d: dict) -> Path:
        if d.get("profile"):
            return self.store.get(str(d["profile"])).path / "user"
        return self.account(d.get("account_id")).cfg_dir

    def _status(self) -> dict:
        if not self.steam:
            banner = ("error", "Steam не найден. Откройте меню ⋯ → «Папка Steam» и укажите папку, где лежит userdata.")
        elif not self.accounts:
            banner = ("error", "В Steam не найдено ни одного аккаунта. Войдите в Steam хотя бы раз и нажмите «Обновить».")
        elif self.game_running is None:
            banner = ("warn", "Не удалось проверить, запущена ли CS2. Перед восстановлением убедитесь сами, что игра закрыта.")
        elif self.game_running:
            banner = ("warn", "CS2 запущена: сохранять можно, восстановление заблокировано. Закройте игру.")
        elif self.steam_running:
            banner = ("info", "Steam запущен. Лучше закрыть его перед восстановлением, иначе Steam Cloud может перезаписать файлы.")
        else:
            banner = ("", "")
        return {"game": self.game_running, "steam": self.steam_running, "banner": banner}

    # ------------------------------------------------------------------ API
    def api_ping(self, d):
        return self._status()

    def api_state(self, d):
        with self.lock:
            if d.get("refresh"):
                self.refresh()
            profiles = self.store.list()
            return {"accounts": [{"id": a.account_id, "name": a.name, "label": a.label, "has_cs2": a.has_cs2}
                                 for a in self.accounts],
                    "selected": self.settings.get("account"), "steam_path": str(self.steam or ""),
                    "game_cfg": str(self.game_cfg or ""), "data_dir": str(DATA_DIR / "profiles"),
                    "profiles": [self._profile_json(p) for p in profiles], "skipped": self.store.skipped,
                    "launch_option": LAUNCH_OPTION, **self._status()}

    def api_select_account(self, d):
        self.settings["account"] = self.account(d.get("account_id")).account_id
        self._save_settings()
        return {}

    def api_source(self, d):
        src = self._source_dir(d)
        out: dict = {"binds": {}, "convars": {}, "machine": {}, "video": {}, "errors": [], "files": []}
        for key, fn, rd in (("binds", core.CLOUD_FILES[0], core.read_binds), ("convars", core.CLOUD_FILES[1], core.read_convars),
                            ("machine", core.USER_FILES[2], core.read_convars), ("video", core.VIDEO_FILE, core.read_video)):
            if (src / fn).is_file():
                out["files"].append(fn)
            try:  # каждый файл отдельно: один повреждённый не прячет остальные
                out[key] = rd(src / fn)
            except core.ConfigError as e:
                out["errors"].append(str(e))
        return out

    def api_save(self, d):
        with self.lock:
            acc = self.account(d.get("account_id"))
            name = core.validate_name(str(d.get("name", "")))
            parts = self._parts(d, ("binds", "settings", "video", "game"))
            game = d.get("game", "autoexec")
            names: tuple[str, ...] = ()
            if "game" in parts:
                names = tuple(core.list_game_cfgs(self.game_cfg)) if game == "all" else ("autoexec.cfg",)
            if (self.store.root / name).exists() and not d.get("overwrite"):
                return {"exists": name}
            p = self.store.save(name, acc, self.game_cfg, str(d.get("note", "")), names, bool(d.get("overwrite")),
                                parts=tuple(x for x in parts if x != "game"))
            return {"profile": self._profile_json(p)}

    def _confirmable(self, fn):
        try:
            done, warns = fn()
        except core.UnknownStateError:
            return {"confirm": "Не удалось проверить, запущена ли CS2. Если игра открыта, Steam Cloud может "
                               "перезаписать результат. Игра точно закрыта?"}
        return {"done": done, "warnings": warns}

    def api_restore(self, d):
        with self.lock:
            p, acc = self.store.get(str(d.get("name"))), self.account(d.get("account_id"))
            parts = self._parts(d)
            return self._confirmable(lambda: self.store.restore(p, acc, self.game_cfg, parts,
                                                                confirm_unknown=bool(d.get("confirm_unknown"))))

    def api_transfer(self, d):
        with self.lock:
            src, dst = self.account(d.get("from")), self.account(d.get("to"))
            parts = self._parts(d, ("binds", "settings", "video"))
            return self._confirmable(lambda: self.store.transfer(src, dst, parts,
                                                                 confirm_unknown=bool(d.get("confirm_unknown"))))

    def api_rename(self, d):
        with self.lock:
            self.store.rename(str(d.get("name")), str(d.get("new", "")))
            return {"name": core.validate_name(str(d.get("new", "")))}

    def api_delete(self, d):
        with self.lock:
            self.store.delete(str(d.get("name")))
            return {}

    def api_compare(self, d):
        p, acc = self.store.get(str(d.get("name"))), self.account(d.get("account_id"))
        return {"sections": core.compare_struct(acc.cfg_dir, p.path / "user")}

    def api_autoexec_build(self, d):
        src = self.api_source(d)
        convars = {**src["machine"], **src["convars"]} if d.get("convars") else {}
        return {"text": core.to_autoexec(src["binds"] if d.get("binds") else {}, convars),
                "errors": src["errors"]}

    def api_autoexec_read(self, d):
        t = self.game_cfg / "autoexec.cfg" if self.game_cfg else None
        if not t or not t.is_file():
            return {"text": "", "exists": False}
        try:
            return {"text": t.read_text(encoding="utf-8", errors="replace")[:core.MAX_FILE], "exists": True}
        except OSError as e:
            raise core.ConfigError(f"Не удалось прочитать autoexec.cfg: {e.strerror or e}") from e

    def api_autoexec_write(self, d):
        text = str(d.get("text", "")).strip()
        if core.autoexec_is_empty(text):
            raise core.ConfigError("В тексте нет ни одной команды. Файл не записан, чтобы не затереть существующий autoexec.cfg.")
        if not self.game_cfg:
            raise core.ConfigError("Папка игры не найдена, поэтому autoexec.cfg записать некуда.")
        target = self.game_cfg / "autoexec.cfg"
        with self.lock:
            if target.exists() and not d.get("confirm"):
                return {"confirm": "autoexec.cfg уже существует. Заменить его? Старый сохранится рядом как .bak."}
            bak = core.write_text_atomic(target, text + "\n", backup=True)
            return {"path": str(target), "backup": bak.name if bak else ""}

    @staticmethod
    def _str_dict(v, what: str) -> dict[str, str]:
        if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()):
            raise core.ConfigError(f"Неверный список: {what}.")
        return v

    def api_cfg_build(self, d):
        """Текст своего .cfg из выбранных биндов, команд и дополнительных строк."""
        text = core.build_cfg(str(d.get("name", "")), self._str_dict(d.get("binds", {}), "бинды"),
                              self._str_dict(d.get("convars", {}), "команды"), str(d.get("extra", "")))
        return {"text": text, "file": core.validate_cfg_name(str(d.get("name", "")))}

    def api_cfg_write(self, d):
        fname = core.validate_cfg_name(str(d.get("name", "")))
        text = self.api_cfg_build(d)["text"]
        if core.autoexec_is_empty(text):
            raise core.ConfigError("Ничего не выбрано: отметьте бинды или команды.")
        if not self.game_cfg:
            raise core.ConfigError("Папка игры не найдена, записать некуда. Используйте «Скачать».")
        target = self.game_cfg / fname
        with self.lock:
            if target.exists() and not d.get("confirm"):
                return {"confirm": f"{fname} уже есть в папке игры. Заменить? Старый сохранится рядом как .bak."}
            bak = core.write_text_atomic(target, text, backup=True)
        return {"path": str(target), "backup": bak.name if bak else "", "exec": fname[:-4]}

    def api_set_steam(self, d):
        path = Path(str(d.get("path", "")).strip().strip('"'))
        if not (path / "userdata").is_dir():
            raise core.ConfigError("В этой папке нет подпапки userdata. Укажите корневую папку Steam.")
        with self.lock:
            self.settings["steam"] = str(path)
            self._save_settings()
            self.refresh()
        return {}

    def api_open(self, d):
        what = d.get("what")
        target = {"user": lambda: self.account(d.get("account_id")).cfg_dir, "game": lambda: self.game_cfg,
                  "data": lambda: self.store.root, "profile": lambda: self.store.get(str(d.get("name"))).path}.get(what)
        path = target() if target else None
        if not path or not Path(path).is_dir():
            raise core.ConfigError("Папка не найдена.")
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        return {}

    def export_zip(self, name: str, anon: bool) -> tuple[str, bytes]:
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        try:
            self.store.export_zip(name, Path(tmp), anonymize=anon)
            return f"{name}.zip", Path(tmp).read_bytes()
        finally:
            os.remove(tmp)

    def import_zip(self, filename: str, data: bytes) -> dict:
        tmpdir = Path(tempfile.mkdtemp())
        try:
            src = tmpdir / (Path(filename).name or "profile.zip")
            src.write_bytes(data)
            with self.lock:
                return {"profile": self._profile_json(self.store.import_zip(src))}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    backend: Backend
    token: str
    port: int
    server_version = "CS2ConfigSaver"

    def log_message(self, fmt, *args):  # без шума в консоли
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        # Защита от DNS rebinding: чужой сайт не сможет обратиться к серверу под своим именем.
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _guard(self, token: str | None) -> bool:
        if not self._host_ok() or not token or not secrets.compare_digest(token, self.token):
            self._send(403, "Доступ запрещён. Запустите CS2 Config Saver заново.".encode("utf-8"), "text/plain; charset=utf-8")
            return False
        self.backend.last_seen = time.time()
        return True

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        tok = (q.get("t") or [None])[0]
        if u.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if not self._guard(tok):
            return
        if u.path == "/":
            html = resource("ui.html").read_bytes()
            csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                   "img-src data:; connect-src 'self'; form-action 'none'; frame-ancestors 'none'")
            return self._send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy": csp})
        if u.path == "/export":
            try:
                fname, data = self.backend.export_zip((q.get("name") or [""])[0], (q.get("anon") or ["1"])[0] == "1")
            except core.ConfigError as e:
                return self._send(400, str(e).encode("utf-8"), "text/plain; charset=utf-8")
            disp = f"attachment; filename=\"profile.zip\"; filename*=UTF-8''{quote(fname)}"
            return self._send(200, data, "application/zip", {"Content-Disposition": disp})
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if not self._guard(self.headers.get("X-Token")):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_UPLOAD:
            return self._json(413, {"error": "Слишком большой запрос."})
        body = self.rfile.read(length)
        try:
            if u.path == "/api/import":
                return self._json(200, self.backend.import_zip(unquote(self.headers.get("X-Filename", "")), body))
            if u.path == "/api/quit":
                self._json(200, {})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._json(415, {"error": "Ожидается JSON."})
            fn = getattr(self.backend, "api_" + u.path.removeprefix("/api/"), None) if u.path.startswith("/api/") else None
            if not fn:
                return self._json(404, {"error": "Неизвестное действие."})
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                return self._json(400, {"error": "Неверный запрос."})
            self._json(200, fn(data))
        except core.ConfigError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log_error(traceback.format_exc())
            self._json(500, {"error": f"Непредвиденная ошибка: {e}. Подробности в {LOG}"})


def find_app_browser() -> str | None:
    """Edge или Chrome: в режиме --app интерфейс выглядит как обычное окно программы."""
    if sys.platform == "win32":
        cands = []
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                          Path(base) / "Google/Chrome/Application/chrome.exe"]
        return next((str(c) for c in cands if c.is_file()), None)
    if sys.platform == "darwin":
        c = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        return str(c) if c.is_file() else None
    return next((shutil.which(n) for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")
                 if shutil.which(n)), None)


def open_window(url: str) -> subprocess.Popen | None:
    exe = find_app_browser()
    if exe:
        try:  # отдельный профиль браузера: окно не смешивается с вашими вкладками и закрывается вместе с программой
            return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={DATA_DIR / 'window'}",
                                     "--window-size=1280,840", "--no-first-run", "--no-default-browser-check",
                                     "--disable-features=Translate"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    webbrowser.open(url)
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser(description=APP)
    ap.add_argument("--port", type=int, default=0, help="порт (по умолчанию случайный)")
    ap.add_argument("--no-window", action="store_true", help="не открывать окно, только напечатать адрес")
    ap.add_argument("--token", default="", help=argparse.SUPPRESS)  # для отладки
    args = ap.parse_args()
    backend = Backend()
    token = args.token or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    Handler.backend, Handler.token, Handler.port = backend, token, port
    url = f"http://127.0.0.1:{port}/?t={token}"

    def watchdog(proc: subprocess.Popen | None):
        started = time.time()
        while True:
            time.sleep(2)
            if proc and proc.poll() is not None:
                # Процесс сразу передал окно уже открытому браузеру: дальше полагаемся на сигналы страницы.
                if time.time() - started < 8:
                    proc = None
                else:  # окно, запущенное нами, закрыто
                    break
            if time.time() - backend.last_seen > HEARTBEAT_TIMEOUT:
                break
        server.shutdown()

    proc = None if args.no_window else open_window(url)
    threading.Thread(target=watchdog, args=(proc,), daemon=True).start()
    print(f"{APP} работает. Если окно не открылось, откройте в браузере:\n{url}\n"
          "Программа закроется сама, когда вы закроете её окно.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        log_error(traceback.format_exc())
        raise
