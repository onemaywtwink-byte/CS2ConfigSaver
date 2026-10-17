"""Ядро CS2 Config Saver: поиск Steam/CS2, разбор .vcfg, профили, восстановление.

Без внешних зависимостей, без GUI. Работает на Windows, Linux, macOS.
"""
from __future__ import annotations

import json
import os
import stat
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from typing import Any
from dataclasses import dataclass, field
from pathlib import Path

# --- Что CS2 хранит и где (по документации Valve и обсуждениям сообщества) ---
# userdata/<id>/730/local/cfg: пользовательские файлы (синхронизируются с Steam Cloud)
# <steam>/steamapps/common/Counter-Strike Global Offensive/game/csgo/cfg: autoexec.cfg и ваши .cfg
CLOUD_FILES = ("cs2_user_keys_0_slot0.vcfg", "cs2_user_convars_0_slot0.vcfg")
USER_FILES = CLOUD_FILES + ("cs2_machine_convars.vcfg",)
VIDEO_FILE = "cs2_video.txt"
LASTCLOUDED = "_lastclouded"
# Эти ключи CS2 пересоздаёт, если они не совпадают с видеокартой, поэтому при переносе
# видеонастроек сохраняем их значения от текущей машины.
VIDEO_KEEP_KEYS = ("VendorID", "DeviceID", "setting.knowndevice", "AutoConfig")
STEAMID64_BASE = 76561197960265728
MAX_FILE = 1_000_000
MAX_FILES = 200
MAX_TOTAL = 10_000_000
KEEP_AUTO = 10
MAX_DEPTH = 64
KEEP_BAK = 4
AUTO_NOTE = "Создан автоматически перед восстановлением"
AUTO_PREFIX = "auto-backup "
INVALID_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class ConfigError(Exception):
    """Ошибка с понятным пользователю текстом."""


class UnknownStateError(ConfigError):
    """Не удалось проверить, запущена ли CS2: нужно подтверждение пользователя."""


class NoFilesError(ConfigError):
    """Нечего сохранять: файлов конфига нет."""


# ----------------------------------------------------------------- KeyValues
def parse_kv(text: str) -> dict:
    """Строгий разбор формата Valve KeyValues. Незакрытые и лишние скобки -> ValueError.

    CS2 не экранирует обратную косую: клавиша \\ записана как "\\". Поэтому сначала читаем
    без экранирования, а если структура не сошлась, пробуем вариант с \\" внутри строк.
    """
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    try:
        return _parse_tokens(re.findall(r'"([^"]*)"|([{}])', text))
    except ValueError as plain:
        try:
            return _parse_tokens(re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text))
        except ValueError:
            raise plain from None


def _parse_tokens(toks: list) -> dict:
    pos = 0

    def block(depth: int) -> dict:
        nonlocal pos
        if depth > MAX_DEPTH:
            raise ValueError("слишком глубокая вложенность")
        out: dict = {}
        while pos < len(toks):
            s_, b_ = toks[pos]
            if b_ == "}":
                if depth == 0:
                    raise ValueError("лишняя «}»")
                pos += 1
                return out
            if b_ == "{":
                raise ValueError("лишняя «{»")
            key = s_
            pos += 1
            if pos >= len(toks) or toks[pos][1] == "}":
                raise ValueError(f"нет значения для ключа «{key}»")
            s2, b2 = toks[pos]
            pos += 1
            out[key] = block(depth + 1) if b2 == "{" else s2
        if depth > 0:
            raise ValueError("не хватает закрывающей «}»")
        return out

    return block(0)


def _ci_get(d: dict, key: str) -> dict:
    for k, v in d.items():
        if k.lower() == key.lower() and isinstance(v, dict):
            return v
    return {}


def read_section(path: Path, section: str) -> dict[str, str]:
    """Возвращает {ключ: значение} из config/<section> (bindings или convars)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        kv = parse_kv(text)
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"Не удалось прочитать {Path(path).name}: {e.strerror or e}") from e
    except ValueError as e:
        raise ConfigError(f"Файл {Path(path).name} повреждён: {e}") from e
    root = _ci_get(kv, "config") or kv
    return {k: v for k, v in _ci_get(root, section).items() if not isinstance(v, dict)}


def read_binds(path: Path) -> dict[str, str]:
    return read_section(path, "bindings")


def read_convars(path: Path) -> dict[str, str]:
    return read_section(path, "convars")


def _q(s: str) -> str:
    """Консоль Source не умеет вложенные кавычки: \\" и " заменяем на '."""
    return s.replace('\\"', "'").replace('"', "'").replace("\n", " ").replace("\r", " ")


def to_autoexec(binds: dict[str, str], convars: dict[str, str], extra: str = "") -> str:
    """Собирает текст autoexec.cfg из биндов и настроек (host_writeconfig в конце)."""
    if not (binds or convars or extra.strip()):
        return ""
    out = ["// Создано CS2 Config Saver", ""]
    if convars:
        out.append("// --- Настройки ---")
        out += [f'{_q(k)} "{_q(v)}"' for k, v in sorted(convars.items())]
        out.append("")
    if binds:
        out.append("// --- Бинды ---")
        for k, v in sorted(binds.items()):
            out.append(f'unbind "{_q(k)}"' if v.strip().lower() == "<unbound>" else f'bind "{_q(k)}" "{_q(v)}"')
        out.append("")
    if extra.strip():
        out += ["// --- Дополнительно ---", extra.strip(), ""]
    out.append("host_writeconfig")
    return "\n".join(out) + "\n"


CFG_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def validate_cfg_name(name: str) -> str:
    """Имя своего .cfg для команды exec: латиница, цифры, _ и -, без пробелов и путей. Возвращает «имя.cfg»."""
    base = str(name).strip()
    if base.lower().endswith(".cfg"):
        base = base[:-4]
    if not CFG_NAME.match(base):
        raise ConfigError("Имя файла: от 1 до 40 латинских букв, цифр, «_» или «-», без пробелов "
                          "(иначе команду exec придётся писать в кавычках).")
    if base.lower() in RESERVED:
        raise ConfigError("Это имя зарезервировано Windows, выберите другое.")
    return base + ".cfg"


def build_cfg(name: str, binds: dict[str, str], convars: dict[str, str], extra: str = "") -> str:
    """Свой .cfg из выбранных биндов и команд. Загружается в игре командой exec <имя>."""
    text = to_autoexec(binds, convars, extra)
    if not text:
        return ""
    base = validate_cfg_name(name)[:-4]
    return text.replace("// Создано CS2 Config Saver",
                        f"// Создано CS2 Config Saver\n// Загрузка в игре: exec {base}", 1)


def autoexec_is_empty(text: str) -> bool:
    """True, если в тексте нет ни одной настоящей команды (только комментарии и host_writeconfig)."""
    return all(not l.strip() or l.strip().startswith("//") or l.strip().lower() == "host_writeconfig"
               for l in text.splitlines())


def read_video(path: Path) -> dict[str, str]:
    """Видеонастройки из cs2_video.txt без GPU-ключей (они у каждого ПК свои)."""
    try:
        kv = parse_kv(Path(path).read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"Не удалось прочитать {Path(path).name}: {e.strerror or e}") from e
    except ValueError as e:
        raise ConfigError(f"Файл {Path(path).name} повреждён: {e}") from e
    root = next((v for v in kv.values() if isinstance(v, dict)), kv)
    skip = {k.lower() for k in VIDEO_KEEP_KEYS}
    return {k: v for k, v in root.items() if not isinstance(v, dict) and k.lower() not in skip}


# --------------------------------------------------------------------- Steam
def find_steam_root(override: str | None = None) -> Path | None:
    cands: list[Path] = []
    if override:
        cands.append(Path(override))
    if sys.platform == "win32":
        try:
            import winreg  # type: ignore

            for hive, sub in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        for name in ("SteamPath", "InstallPath"):
                            try:
                                cands.append(Path(winreg.QueryValueEx(k, name)[0]))
                            except OSError:
                                pass
                except OSError:
                    pass
        except ImportError:
            pass
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                cands.append(Path(os.environ[env]) / "Steam")
    else:
        home = Path.home()
        cands += [home / ".var/app/com.valvesoftware.Steam/.local/share/Steam", home / ".steam" / "steam", home / ".local" / "share" / "Steam",
                  home / "Library" / "Application Support" / "Steam"]
    for c in cands:
        if (c / "userdata").is_dir():
            return c
    return None


def find_game_cfg(steam: Path) -> Path | None:
    libs = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    try:
        for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")):
            libs.append(Path(m.group(1).replace("\\\\", "\\")))
    except OSError:
        pass
    for lib in libs:
        p = lib / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game" / "csgo" / "cfg"
        if p.is_dir():
            return p
    return None


@dataclass
class Account:
    account_id: str
    cfg_dir: Path
    name: str = ""
    login: str = ""

    @property
    def has_cs2(self) -> bool:
        """Есть ли на этом ПК конфиг CS2 (папка появляется после первого запуска игры)."""
        return self.cfg_dir.is_dir()

    @property
    def label(self) -> str:
        base = self.name or self.login
        if self.name and self.login and self.name.lower() != self.login.lower():
            base = f"{self.name} · {self.login}"
        return f"{base} ({self.account_id})" if base else self.account_id


def _visible(s: str) -> str:
    """Ник из одних невидимых/комбинируемых символов считаем пустым."""
    s = (s or "").strip()
    return s if any(ch.isalnum() for ch in s) else ""


def find_accounts(steam: Path) -> list[Account]:
    """Все аккаунты Steam на этом ПК: из userdata и из списка входа, даже если CS2 на них ещё не запускалась."""
    users: dict[str, tuple[str, str]] = {}
    try:
        kv = parse_kv((steam / "config" / "loginusers.vdf").read_text(encoding="utf-8", errors="replace"))
        for sid, info in _ci_get(kv, "users").items():
            if sid.isdigit() and int(sid) > STEAMID64_BASE and isinstance(info, dict):
                users[str(int(sid) - STEAMID64_BASE)] = (_visible(str(info.get("PersonaName", ""))),
                                                         _visible(str(info.get("AccountName", ""))))
    except (OSError, ValueError):
        pass
    ud = steam / "userdata"
    ids = set(users)
    try:
        ids |= {d.name for d in ud.iterdir() if d.is_dir() and d.name.isdigit() and d.name != "0"}
    except OSError:
        pass
    out = []
    for aid in ids:
        name, login = users.get(aid, ("", ""))
        out.append(Account(aid, ud / aid / "730" / "local" / "cfg", name, login))
    # сначала аккаунты с конфигом CS2, внутри по имени
    return sorted(out, key=lambda a: (not a.has_cs2, (a.name or a.login or "~").lower(), a.account_id))


def is_running(*procs: str):
    """True/False, а если проверить не удалось, то None (защита не должна молча отключаться)."""
    kw: dict[str, Any] = dict(capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
    try:
        if sys.platform == "win32":
            for p in procs:
                r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {p}", "/NH"], creationflags=0x08000000, **kw)
                if p.lower().encode() in r.stdout.lower() or p.lower() in r.stdout.decode("utf-8", "replace").lower():
                    return True
            return False
        for p in procs:
            if subprocess.run(["pgrep", "-x", p.removesuffix(".exe")], **kw).returncode == 0:
                return True
        return False
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ файлы
def _clear_readonly(dst: Path) -> int | None:
    """На Windows os.replace падает поверх файла «только чтение». Снимаем атрибут и возвращаем
    исходный режим, чтобы после замены вернуть его в точности (None, если файл был доступен для записи)."""
    try:
        mode = stat.S_IMODE(dst.stat().st_mode)
        if dst.is_file() and not (mode & stat.S_IWRITE):
            os.chmod(dst, mode | stat.S_IWRITE)
            return mode
    except OSError:
        pass
    return None


def _restore_readonly(dst: Path, mode: int | None) -> None:
    if mode is not None:
        try:
            os.chmod(dst, mode)
        except OSError:
            pass


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    ro = _clear_readonly(dst)
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=".tmp_")
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        _restore_readonly(dst, ro)
        if os.path.exists(tmp):
            os.remove(tmp)


def write_text_atomic(dst: Path, text: str, backup: bool = False) -> Path | None:
    """Атомарная запись. С backup=True старый файл сохраняется как <имя>.bak. Возвращает путь бэкапа."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    bak = None
    ro = _clear_readonly(dst)
    if backup and dst.is_file():
        bak = dst.with_name(dst.name + ".bak")
        if bak.exists():  # первый .bak (оригинал) не затираем
            bak = dst.with_name(f"{dst.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
            n = 2
            while bak.exists():
                bak = dst.with_name(f"{dst.name}.{time.strftime('%Y%m%d-%H%M%S')}-{n}.bak")
                n += 1
        shutil.copyfile(dst, bak)
        stamped = sorted(dst.parent.glob(dst.name + ".*.bak"), key=lambda p: p.stat().st_mtime)
        for old in stamped[:-KEEP_BAK]:  # .bak с оригиналом остаётся, лишние датированные удаляем
            old.unlink(missing_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, dst)
    finally:
        _restore_readonly(dst, ro)
        if os.path.exists(tmp):
            os.remove(tmp)
    return bak


def list_game_cfgs(game_cfg: Path | None) -> list[str]:
    if not game_cfg or not game_cfg.is_dir():
        return []
    return sorted(p.name for p in game_cfg.glob("*.cfg") if p.is_file() and p.stat().st_size < MAX_FILE)


def diff_sections(old: dict[str, str], new: dict[str, str]) -> list[str]:
    out = []
    for k in sorted(set(old) | set(new), key=str.lower):
        if k not in old:
            out.append(f"+ {k} = {new[k]}")
        elif k not in new:
            out.append(f"- {k} = {old[k]}")
        elif old[k] != new[k]:
            out.append(f"~ {k}: {old[k]} -> {new[k]}")
    return out


def merge_video(new_text: str, current_text: str) -> str:
    """Переносит видеонастройки, но оставляет GPU-ключи текущей машины."""
    for key in VIDEO_KEEP_KEYS:
        m = re.search(rf'"{re.escape(key)}"\s+"([^"]*)"', current_text, flags=re.I)
        if m:
            val = m.group(1)
            new_text = re.sub(rf'("{re.escape(key)}"\s+)"[^"]*"', lambda mm: f'{mm.group(1)}"{val}"',  # noqa: B023 (вызывается сразу, до смены val)
                              
                              new_text, flags=re.I)
    return new_text


def sibling_video(cfg_dir: Path) -> Path | None:
    """cs2_video.txt другого аккаунта на этом же ПК (самый свежий): видеокарта у них та же,
    поэтому GPU-ключи из него подходят аккаунту, у которого своего файла ещё нет."""
    own = Path(cfg_dir)
    try:
        userdata = own.parents[3]  # <userdata>/<id>/730/local/cfg
        cands = [p for p in userdata.glob(f"*/730/local/cfg/{VIDEO_FILE}") if p.parent != own and p.is_file()]
        return max(cands, key=lambda p: p.stat().st_mtime) if cands else None
    except (OSError, IndexError):
        return None


def compare_struct(cur: Path, prof: Path) -> list[dict]:
    """Сравнение по разделам: [{title, file, missing, error, diff: [{op, key, old, new}]}], текущий -> профиль."""
    out = []
    plan = (("Бинды", CLOUD_FILES[0], read_binds), ("Настройки", CLOUD_FILES[1], read_convars),
            ("Системные настройки", USER_FILES[2], read_convars), ("Видео", VIDEO_FILE, read_video))
    for title, fn, rd in plan:
        sec: dict = {"title": title, "file": fn, "missing": not (prof / fn).is_file(), "error": "", "diff": []}
        if not sec["missing"]:
            try:
                old, new = (rd(cur / fn) if (cur / fn).is_file() else {}), rd(prof / fn)
            except ConfigError as e:
                sec["error"] = str(e)
            else:
                for k in sorted(set(old) | set(new), key=str.lower):
                    if old.get(k) != new.get(k):
                        op = "+" if k not in old else "-" if k not in new else "~"
                        sec["diff"].append({"op": op, "key": k, "old": old.get(k), "new": new.get(k)})
        out.append(sec)
    return out


def compare_dirs(cur: Path, prof: Path) -> list[str]:
    """Текст сравнения: что изменится, если применить профиль (текущий -> профиль)."""
    out = []
    for s in compare_struct(cur, prof):
        if s["missing"]:
            out += [f"── {s['title']}: в профиле нет файла {s['file']}, при восстановлении не изменится ──", ""]
        elif s["error"]:
            out += [f"── {s['title']}: {s['error']} ──", ""]
        else:
            diff = [f"+ {d['key']} = {d['new']}" if d["op"] == "+" else f"- {d['key']} = {d['old']}" if d["op"] == "-"
                    else f"~ {d['key']}: {d['old']} -> {d['new']}" for d in s["diff"]]
            out += [f"── {s['title']}: различий {len(diff)} ──"] + (diff or ["без отличий"]) + [""]
    return out


# ----------------------------------------------------------------- профили
PARTS = {"binds": (CLOUD_FILES[0],), "settings": (CLOUD_FILES[1], USER_FILES[2]), "video": (VIDEO_FILE,)}
PART_TITLES = {"binds": "Бинды", "settings": "Настройки", "video": "Видео", "game": "autoexec / .cfg"}


def part_files(parts) -> tuple[str, ...]:
    """Файлы аккаунта для выбранных частей. «user» из прошлой версии = бинды + настройки."""
    parts = set(parts)
    if "user" in parts:
        parts |= {"binds", "settings"}
    return tuple(fn for p, fns in PARTS.items() if p in parts for fn in fns)


@dataclass
class Profile:
    name: str
    path: Path
    created: str = ""
    note: str = ""
    account_id: str = ""
    files: list[str] = field(default_factory=list)
    imported: bool = False
    auto: bool = False

    @property
    def is_auto(self) -> bool:
        return self.auto

    @property
    def parts(self) -> list[str]:
        """Какие части есть в профиле, в порядке PART_TITLES."""
        have = {f.split("/", 1)[-1] for f in self.files}
        out = [p for p, fns in PARTS.items() if any(fn in have for fn in fns)]
        return out + (["game"] if any(f.startswith("game/") for f in self.files) else [])


def validate_name(name: str) -> str:
    name = name.strip().rstrip(". ")
    if not name:
        raise ConfigError("Введите название профиля.")
    if len(name) > 80:
        raise ConfigError("Название слишком длинное (максимум 80 символов).")
    if INVALID_NAME.search(name):
        raise ConfigError('В названии нельзя использовать символы \\ / : * ? " < > |')
    if name.lower().startswith(AUTO_PREFIX.strip().lower()):
        raise ConfigError("Названия, начинающиеся с «auto-backup», зарезервированы для автобэкапов.")
    if name.split(".")[0].strip().lower() in RESERVED:
        raise ConfigError("Это имя зарезервировано Windows, выберите другое.")
    return name


def clean_meta(m) -> dict | None:
    """Проверяет meta.json: dict, строковые поля, files = список строк. Иначе None."""
    if not isinstance(m, dict):
        return None
    files = m.get("files", [])
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        return None
    for k in ("created", "note", "account_id", "name"):
        if not isinstance(m.get(k, ""), str):
            return None
    return {"name": m.get("name", ""), "note": m.get("note", ""), "account_id": m.get("account_id", ""),
            "created": m.get("created", ""), "files": files, "imported": bool(m.get("imported")),
            "auto": bool(m.get("auto"))}


class ProfileStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    skipped = 0  # сколько профилей не удалось прочитать при последнем list()

    def list(self, include_auto: bool = True) -> list[Profile]:
        out = []
        self.skipped = 0
        for d in sorted(self.root.iterdir()):
            meta = d / "meta.json"
            if d.name.startswith(".") or not (d.is_dir() and meta.is_file()):
                continue
            try:
                raw = json.loads(meta.read_text(encoding="utf-8"))
                m = clean_meta(raw)
            except (OSError, ValueError, RecursionError):
                raw, m = None, None
            if m is None:  # битый профиль пропускаем, остальные работают
                self.skipped += 1
                continue
            if "auto" not in raw and d.name.startswith(AUTO_PREFIX) and m["note"] == AUTO_NOTE:
                m["auto"] = True  # миграция: автобэкапы прошлой версии не имели поля auto
            p = Profile(d.name, d, m["created"], m["note"], m["account_id"], m["files"], m["imported"], m["auto"])
            if include_auto or not p.auto:
                out.append(p)
        return sorted(out, key=lambda p: p.created, reverse=True)

    def get(self, name: str) -> Profile:
        for p in self.list():
            if p.name == name:
                return p
        raise ConfigError(f"Профиль «{name}» не найден.")

    def save(self, name: str, account: Account, game_cfg: Path | None, note: str = "",
             game_names: tuple[str, ...] = ("autoexec.cfg",), overwrite: bool = False,
             _internal: bool = False, _imported: bool = False,
             parts: tuple[str, ...] = ("binds", "settings", "video")) -> Profile:
        """parts выбирает, какие файлы аккаунта попадут в профиль (например, только бинды)."""
        name = name if _internal else validate_name(name)
        dest = self.root / name
        if dest.exists() and not overwrite:
            raise ConfigError(f"Профиль «{name}» уже существует.")
        tmp = self.root / f".building_{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        files: list[str] = []
        try:
            for fn in part_files(parts):
                src = account.cfg_dir / fn
                if src.is_file():
                    _atomic_copy(src, tmp / "user" / fn)
                    files.append(f"user/{fn}")
            if game_cfg and game_cfg.is_dir():
                for fn in game_names:
                    src = game_cfg / fn
                    if src.is_file() and src.stat().st_size < MAX_FILE and Path(fn).name == fn:
                        _atomic_copy(src, tmp / "game" / fn)
                        files.append(f"game/{fn}")
            if not files:
                raise NoFilesError("Не найдено ни одного файла конфига. Запустите CS2 хотя бы раз и "
                                   "выберите нужный аккаунт Steam.")
            (tmp / "meta.json").write_text(json.dumps({
                "name": name, "note": note.strip(), "account_id": account.account_id,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"), "files": files, "imported": _imported,
                "auto": _internal}, ensure_ascii=False, indent=2), encoding="utf-8")
            self._swap_in(tmp, dest)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return self.get(name)

    @staticmethod
    def _swap_in(tmp: Path, dest: Path) -> None:
        """Старый профиль удаляется только после успешной замены."""
        old = dest.with_name(f".old_{os.getpid()}")
        shutil.rmtree(old, ignore_errors=True)
        had = dest.exists()
        if had:
            os.replace(dest, old)
        try:
            os.replace(tmp, dest)
        except Exception:
            if had:
                os.replace(old, dest)
            raise
        shutil.rmtree(old, ignore_errors=True)

    def _auto_backup(self, account: Account, game_cfg: Path | None, game_names: tuple[str, ...]) -> Profile | None:
        base = f"{AUTO_PREFIX}{time.strftime('%Y-%m-%d %H-%M-%S')}"
        name, i = base, 2
        while (self.root / name).exists():
            name = f"{base} ({i})"
            i += 1
        try:
            made = self.save(name, account, game_cfg, AUTO_NOTE, game_names,
                             _internal=True)
        except NoFilesError:
            made = None  # у нового ПК текущего конфига ещё нет: бэкапить нечего
        autos = [p for p in self.list() if p.is_auto]
        for old in autos[KEEP_AUTO:]:  # только профили с флагом auto, пользовательские не трогаем
            shutil.rmtree(old.path, ignore_errors=True)
        return made

    @staticmethod
    def _rollback(snapshot: dict[Path, bytes | None]) -> None:
        """Возвращает каждый затронутый файл к снимку: прежнее содержимое или удаление, если файла не было."""
        errors = []
        for t, data in snapshot.items():
            try:
                if data is None:
                    t.unlink(missing_ok=True)
                else:
                    ro = _clear_readonly(t)
                    try:
                        fd, tmp = tempfile.mkstemp(dir=t.parent, prefix=".tmp_")
                        with os.fdopen(fd, "wb") as f:
                            f.write(data)
                        os.replace(tmp, t)
                    finally:
                        _restore_readonly(t, ro)
            except OSError as e:
                errors.append(f"{t.name}: {e.strerror or e}")
        if errors:
            raise OSError("; ".join(errors))

    def restore(self, profile: Profile, account: Account, game_cfg: Path | None,
                parts: tuple[str, ...] = ("binds", "settings", "video", "game"), running_check=is_running,
                confirm_unknown: bool = False):
        """Применяет профиль. Все проверки идут ДО первой записи. Возвращает (файлы, предупреждения).

        parts: любые из binds, settings, video, game (старое «user» = binds + settings)."""
        state = running_check("cs2.exe")
        if state:
            raise ConfigError("CS2 сейчас запущена. Закройте игру (и лучше Steam), иначе Steam Cloud "
                              "перезапишет восстановленные файлы.")
        if state is None and not confirm_unknown:
            raise UnknownStateError("Не удалось проверить, запущена ли CS2.")
        user_src = profile.path / "user"
        game_src = sorted((profile.path / "game").glob("*.cfg")) if "game" in parts else []
        if game_src and not game_cfg:
            raise ConfigError("Папка игры не найдена, поэтому autoexec и .cfg восстановить нельзя. "
                              "Снимите эту галочку или укажите папку Steam.")
        warnings: list[str] = []
        do_video = "video" in parts and (user_src / VIDEO_FILE).is_file()
        cur_video = account.cfg_dir / VIDEO_FILE
        # GPU-ключи берём из своего файла, а если его нет, из файла другого аккаунта на этом ПК.
        gpu_ref = cur_video if cur_video.is_file() else (sibling_video(account.cfg_dir) if do_video else None)
        if do_video and gpu_ref is None:
            do_video = False
            warnings.append("Видео пропущено: на этом ПК ещё нет ни одного cs2_video.txt. Запустите CS2 один раз, "
                            "чтобы игра создала его с вашей видеокартой, и повторите.")
        do_user = [fn for fn in part_files(parts) if fn != VIDEO_FILE and (user_src / fn).is_file()]
        if not (do_user or do_video or game_src):
            if warnings:  # например, выбрано только видео, а локального файла нет
                return [], warnings
            raise ConfigError("В профиле нет файлов для выбранных частей.")
        backup = self._auto_backup(account, game_cfg, tuple(p.name for p in game_src))
        done: list[str] = []
        targets = [account.cfg_dir / fn for fn in do_user] + \
                  [account.cfg_dir / (fn + LASTCLOUDED) for fn in do_user if fn in CLOUD_FILES] + \
                  ([cur_video] if do_video else []) + ([game_cfg / g.name for g in game_src] if game_cfg else [])
        try:  # снимок до записи: откат вернёт ровно эти байты, а созданные файлы удалит
            snapshot = {t: (t.read_bytes() if t.is_file() else None) for t in targets}
        except OSError as e:
            raise ConfigError(f"Не удалось прочитать текущие файлы перед записью: {e.strerror or e}") from e
        try:
            for fn in do_user:
                _atomic_copy(user_src / fn, account.cfg_dir / fn)
                if fn in CLOUD_FILES:  # тот же текст в _lastclouded, чтобы Steam Cloud не откатил
                    _atomic_copy(user_src / fn, account.cfg_dir / (fn + LASTCLOUDED))
                done.append(fn)
            if do_video:
                new = (user_src / VIDEO_FILE).read_text(encoding="utf-8", errors="replace")
                assert gpu_ref is not None
                write_text_atomic(cur_video, merge_video(new, gpu_ref.read_text(encoding="utf-8", errors="replace")))
                done.append(VIDEO_FILE)
            for s_ in game_src:
                assert game_cfg is not None
                _atomic_copy(s_, game_cfg / s_.name)
                done.append(s_.name)
        except Exception as e:
            try:
                self._rollback(snapshot)
            except Exception as e2:  # noqa: BLE001
                tail = f" Бэкап: «{backup.name}»." if backup else ""
                raise ConfigError(f"Запись не удалась ({e}), и откат тоже ({e2}).{tail}") from e
            raise ConfigError(f"Запись не удалась ({e}). Изменения отменены: изменённые файлы возвращены, "
                              "созданные операцией удалены.") from e
        return done, warnings

    def transfer(self, src: Account, dst: Account, parts: tuple[str, ...] = ("binds",),
                 running_check=is_running, confirm_unknown: bool = False):
        """Переносит части конфига (например, только бинды) с одного аккаунта на другой.
        Работает как восстановление: проверки, автобэкап аккаунта-получателя, откат при сбое."""
        if src.account_id == dst.account_id:
            raise ConfigError("Выберите другой аккаунт: источник и получатель совпадают.")
        tmp = self.root / f".transfer_{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            files = []
            for fn in part_files(parts):
                if (src.cfg_dir / fn).is_file():
                    _atomic_copy(src.cfg_dir / fn, tmp / "user" / fn)
                    files.append(f"user/{fn}")
            if not files:
                raise ConfigError(f"У аккаунта {src.label} нет файлов для выбранных частей.")
            return self.restore(Profile("transfer", tmp, files=files, account_id=src.account_id), dst, None,
                                tuple(parts), running_check, confirm_unknown)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def delete(self, name: str) -> None:
        p = self.get(name)
        shutil.rmtree(p.path)

    def rename(self, old: str, new: str) -> None:
        new = validate_name(new)
        p = self.get(old)
        dest = self.root / new
        if dest.exists():
            raise ConfigError(f"Профиль «{new}» уже существует.")
        os.replace(p.path, dest)
        meta = dest / "meta.json"
        m = clean_meta(json.loads(meta.read_text(encoding="utf-8"))) or {}
        m["name"] = new
        meta.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")

    def export_zip(self, name: str, target: Path, anonymize: bool = True) -> None:
        """По умолчанию из meta.json убираются account_id (по нему вычисляется SteamID64) и заметка."""
        p = self.get(name)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for f in p.path.rglob("*"):
                if f.is_file() and f.name != "meta.json":
                    z.write(f, f.relative_to(p.path).as_posix())
            m = clean_meta(json.loads((p.path / "meta.json").read_text(encoding="utf-8"))) or {}
            if anonymize:
                m.update(account_id="", note="")
            z.writestr("meta.json", json.dumps(m, ensure_ascii=False, indent=2))

    def import_zip(self, source: Path, name: str | None = None) -> Profile:
        try:
            z = zipfile.ZipFile(source)
        except (zipfile.BadZipFile, OSError) as e:
            raise ConfigError("Не удалось открыть ZIP: файл повреждён или недоступен.") from e
        with z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_TOTAL or \
                    any(i.file_size > MAX_FILE for i in infos):
                raise ConfigError("Архив слишком большой для профиля конфига, импорт отменён.")
            names = [i.filename for i in infos]
            if "meta.json" not in names:
                raise ConfigError("Это не архив профиля CS2 Config Saver (нет meta.json).")
            for n in names:
                p = Path(n)
                ok = n == "meta.json" or (len(p.parts) == 2 and p.parts[0] in ("user", "game")
                                          and not p.is_absolute() and ".." not in p.parts and ":" not in n
                                          and (p.parts[0] == "user" and n.split("/")[1] in USER_FILES + (VIDEO_FILE,)
                                               or p.parts[0] == "game" and p.suffix.lower() == ".cfg"))
                if not ok:
                    raise ConfigError(f"Неожиданный или небезопасный файл в архиве: {n}")
            try:
                meta = clean_meta(json.loads(z.read("meta.json").decode("utf-8")))
            except (ValueError, RecursionError) as e:
                raise ConfigError("meta.json в архиве повреждён.") from e
            if meta is None:
                raise ConfigError("meta.json в архиве имеет неверную структуру, импорт отменён.")
            raw = name or meta.get("name") or Path(source).stem
            if str(raw).lower().startswith(AUTO_PREFIX.strip().lower()):
                raw = "импорт " + str(raw)  # зарезервированный префикс автобэкапов
            name = validate_name(raw)
            base, i = name, 2
            while (self.root / name).exists():
                name = f"{base} ({i})"
                i += 1
            tmp = self.root / f".import_{os.getpid()}"
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                z.extractall(tmp)
                meta.update(name=name, imported=True, auto=False, created=time.strftime("%Y-%m-%d %H:%M:%S"),
                            files=[n for n in names if n != "meta.json"])
                (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                self._swap_in(tmp, self.root / name)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return self.get(name)
