#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cyberpunk 2077 Mod Bulucu ve Silici
===================================
* Oyun klasörünü otomatik bulur (Steam / GOG / Epic / Proton), bulamazsa elle seçtirir.
* Yalnızca BİLİNEN mod klasörlerini tarar; oyunun kendi dosyalarına hiç dokunmaz.
* Bulunan modların adını, sayısını, dosya sayısını ve boyutunu gösterir.
* Seçilen modları siler (varsayılan: geri alınabilir "yedeğe taşı").
* Silinen her modu geçmişe kaydeder (gecmis.json + gecmis.txt) ve yedekten geri yüklenebilir.

Gerekli: Python 3.8+  (ek paket gerekmez).   Arayüz: tkinter (yoksa otomatik metin moduna geçer)
Kullanım:  cp2077_mod_silici.pyw'ye çift tıkla      -> arayüz (siyah pencere açılmaz)
           python cp2077_mod_silici.pyw --cli     -> metin modu
Hatalar:   ~/CP2077_ModSilici/hata.log
           python cp2077_mod_silici.pyw --yol "D:\\Games\\Cyberpunk 2077"
"""
import argparse
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

APP_NAME = "Cyberpunk 2077 Mod Bulucu ve Silici"
VERSION = "2.0"

DATA_DIR = Path(os.environ.get("CP2077_MODSILICI_DATA") or (Path.home() / "CP2077_ModSilici"))
HISTORY_JSON = DATA_DIR / "gecmis.json"
HISTORY_TXT = DATA_DIR / "gecmis.txt"
CONFIG_JSON = DATA_DIR / "ayar.json"
BACKUP_DIR = DATA_DIR / "yedek"

# --------------------------------------------------------------------------------------
# Bilinen mod konumları (oyun klasörüne göre). Tarama SADECE buralara bakar.
#   mod türü:  "dirs" = yalnızca alt klasörler,  "any" = klasörler + tek dosyalar,
#              "dll"  = klasörler + .dll dosyaları, "asi" = yalnızca .asi dosyaları
# Kaynak: redmodding.org "Using Mods" / "Core Mods explained" sayfaları
# --------------------------------------------------------------------------------------
SCAN_ROOTS = [
    ("archive/pc/mod", "Arşiv modu (.archive/.xl)", "any"),
    ("r6/scripts", "Redscript (.reds)", "any"),
    ("r6/tweaks", "TweakXL (.yaml/.xl)", "any"),
    ("bin/x64/plugins/cyber_engine_tweaks/mods", "Cyber Engine Tweaks (Lua)", "dirs"),
    ("red4ext/plugins", "RED4ext eklentisi (.dll)", "dll"),
    ("mods", "REDmod", "dirs"),
    ("bin/x64/plugins", "ASI eklentisi (.asi)", "asi"),
]

# Altyapı (framework) bileşenleri: gösterilir ama ASLA silinmez (başka modlar bunlara bağlı).
FRAMEWORK_KEYS = ("archivexl", "tweakxl", "codeware", "redscript", "red4ext", "cybercmd",
                  "cyberenginetweaks", "nativesettings", "inputloader", "modsettings", "equipmentex")

# Ek güvenlik: bu yollar (oyun klasörüne göre) hiçbir koşulda silinmez.
PROTECTED_PREFIXES = ("archive/pc/content", "archive/pc/ep1", "archive/pc/patch", "engine",
                      "r6/cache", "r6/config", "r6/audioconfig", "tools", "support", "launcher",
                      "bin/x64/cyberpunk2077.exe")

IGNORE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "__folder_managed_by_vortex"}
STEM_SUFFIXES = (".archive.xl", ".archive", ".xl", ".reds", ".yaml", ".yml", ".tweak", ".dll", ".asi")


class SafetyError(Exception):
    pass


# --------------------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------------------
def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024


def norm_key(name):
    k = re.sub(r"[^a-z0-9]", "", name.lower())
    return k or name.lower()


def stem_of(fname):
    low = fname.lower()
    for suf in STEM_SUFFIXES:
        if low.endswith(suf):
            return fname[:-len(suf)]
    return os.path.splitext(fname)[0]


def is_framework(name):
    k = norm_key(name)
    return any(k.startswith(f) for f in FRAMEWORK_KEYS)


def _n(p):
    return os.path.normcase(os.path.abspath(str(p)))


def is_inside(child, parent):
    c, p = _n(child), _n(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


def ci_child(parent, name):
    """parent içinde name'i büyük/küçük harf fark etmeden bulur (Linux/Proton için)."""
    parent = Path(parent)
    direct = parent / name
    if os.path.lexists(direct):
        return direct
    try:
        low = name.lower()
        for c in os.listdir(parent):
            if c.lower() == low:
                return parent / c
    except OSError:
        pass
    return None


def ci_path(root, rel):
    cur = Path(root)
    for part in rel.split("/"):
        cur = ci_child(cur, part)
        if cur is None:
            return None
    return cur


def is_game_dir(p):
    try:
        p = Path(p)
        return bool(p.is_dir() and ci_path(p, "bin/x64/Cyberpunk2077.exe") and ci_path(p, "archive/pc/content"))
    except OSError:
        return False


def is_link(p):
    try:
        return os.path.islink(p) or (hasattr(os.path, "isjunction") and os.path.isjunction(p))
    except OSError:
        return False


def _lp(p):
    """Windows'ta 260 karakter yol sınırını aşmak için \\\\?\\ önekli mutlak yol döndürür (diğer sistemlerde aynen)."""
    p = os.path.abspath(str(p))
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        if p.startswith("\\\\"):
            return "\\\\?\\UNC\\" + p[2:]
        return "\\\\?\\" + p
    return p


def _unique(p):
    """Yedek hedefi zaten varsa üzerine/içine taşımamak için _2, _3 ... ekler."""
    p = Path(p)
    if not os.path.lexists(_lp(p)):
        return p
    i = 2
    while os.path.lexists(_lp(p.with_name("%s_%d" % (p.name, i)))):
        i += 1
    return p.with_name("%s_%d" % (p.name, i))


def walk_stats(path):
    """(dosya sayısı, toplam bayt) — sembolik bağlantıları takip etmez."""
    path = Path(path)
    if is_link(path) or path.is_file():
        try:
            return 1, (0 if is_link(path) else path.stat().st_size)
        except OSError:
            return 1, 0
    files = size = 0
    for r, _dirs, fs in os.walk(_lp(path), followlinks=False):
        for f in fs:
            files += 1
            try:
                fp = os.path.join(r, f)
                if not os.path.islink(fp):
                    size += os.path.getsize(fp)
            except OSError:
                pass
    return files, size


# --------------------------------------------------------------------------------------
# Oyun klasörünü bulma
# --------------------------------------------------------------------------------------
def _steam_libraries():
    libs = []
    steam_roots = []
    if os.name == "nt":
        try:
            import winreg
            for hive, key, val in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                   (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                                   (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        steam_roots.append(Path(winreg.QueryValueEx(k, val)[0]))
                except OSError:
                    pass
        except ImportError:
            pass
    else:
        home = Path.home()
        steam_roots += [home / ".steam/steam", home / ".local/share/Steam",
                        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    for sr in steam_roots:
        libs.append(sr)
        vdf = sr / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                libs.append(Path(m.group(1).replace("\\\\", "\\")))
        except OSError:
            pass
    return libs


def find_game_dirs():
    cands = []
    for lib in _steam_libraries():
        cands.append(lib / "steamapps" / "common" / "Cyberpunk 2077")
    if os.name == "nt":
        for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            base = Path(d + ":\\")
            if not base.exists():
                continue
            for sub in (r"Program Files (x86)\Steam\steamapps\common\Cyberpunk 2077",
                        r"SteamLibrary\steamapps\common\Cyberpunk 2077",
                        r"Steam\steamapps\common\Cyberpunk 2077",
                        r"Program Files (x86)\GOG Galaxy\Games\Cyberpunk 2077",
                        r"GOG Games\Cyberpunk 2077",
                        r"Program Files\GOG Galaxy\Games\Cyberpunk 2077",
                        r"Program Files\Epic Games\Cyberpunk2077",
                        r"Epic Games\Cyberpunk2077",
                        r"Games\Cyberpunk 2077"):
                cands.append(base / sub)
    seen, out = set(), []
    for c in cands:
        k = _n(c)
        if k not in seen:
            seen.add(k)
            if is_game_dir(c):
                out.append(c)
    return out


# --------------------------------------------------------------------------------------
# Tarama
# --------------------------------------------------------------------------------------
class Part:
    def __init__(self, root_rel, label, path):
        self.root_rel, self.label, self.path = root_rel, label, Path(path)


class ModItem:
    def __init__(self, key, name):
        self.key, self.name = key, name
        self.parts = []
        self.framework = False
        self.files = 0
        self.size = 0

    @property
    def labels(self):
        out = []
        for p in self.parts:
            if p.label not in out:
                out.append(p.label)
        return out

    def rel_paths(self, game_root):
        out = []
        for p in self.parts:
            try:
                out.append(Path(os.path.relpath(p.path, game_root)).as_posix())
            except ValueError:
                out.append(str(p.path))
        return out


class ScanResult:
    def __init__(self, game_root):
        self.game_root = Path(game_root)
        self.mods = []
        self.frameworks = []
        self.roots = []      # (rel, Path)
        self.warnings = []

    @property
    def total_files(self):
        return sum(m.files for m in self.mods)

    @property
    def total_size(self):
        return sum(m.size for m in self.mods)


def scan(game_root):
    game_root = Path(game_root)
    if not is_game_dir(game_root):
        raise SafetyError("Bu klasör Cyberpunk 2077 oyun klasörü gibi görünmüyor "
                          "(bin/x64/Cyberpunk2077.exe ve archive/pc/content bulunamadı).")
    res = ScanResult(game_root)
    merged = {}

    def add(name, path, rel, label):
        key = norm_key(name)
        it = merged.get(key)
        if it is None:
            it = merged[key] = ModItem(key, name)
        it.parts.append(Part(rel, label, path))

    for rel, label, mode in SCAN_ROOTS:
        root = ci_path(game_root, rel)
        if root is None or not root.is_dir():
            continue
        res.roots.append((rel, root))
        try:
            entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
        except OSError as e:
            res.warnings.append("%s okunamadı: %s" % (rel, e))
            continue
        loose = {}
        for e in entries:
            name = e.name
            low = name.lower()
            if low in IGNORE_NAMES or low.startswith("vortex.deployment") or name.startswith("."):
                continue
            try:
                isdir = e.is_dir()
            except OSError:
                continue
            if mode == "asi":
                if not isdir and low.endswith(".asi"):
                    add(stem_of(name), root / name, rel, label)
                continue
            if isdir:
                add(name, root / name, rel, label)
                continue
            if mode == "dirs":
                continue
            if mode == "dll" and not low.endswith(".dll"):
                continue
            st = stem_of(name)
            loose.setdefault(st.lower(), (st, []))[1].append(root / name)
        for _k, (st, paths) in loose.items():
            for p in paths:
                add(st, p, rel, label)

    # Vortex / MO2 uyarısı
    try:
        if any(game_root.glob("vortex.deployment*.json")) or any(
                (r / "__folder_managed_by_vortex").exists() for _rel, r in res.roots):
            res.warnings.append("Vortex algılandı: modları Vortex'ten kaldırmak daha güvenlidir. Buradan silersen "
                                "Vortex'te tekrar 'Deploy' etmen gerekebilir.")
    except OSError:
        pass

    for it in merged.values():
        it.framework = is_framework(it.name)
        for p in it.parts:
            f, s = walk_stats(p.path)
            it.files += f
            it.size += s
        (res.frameworks if it.framework else res.mods).append(it)
    res.mods.sort(key=lambda m: m.name.lower())
    res.frameworks.sort(key=lambda m: m.name.lower())
    return res


# --------------------------------------------------------------------------------------
# Güvenlik doğrulaması
# --------------------------------------------------------------------------------------
def validate_target(game_root, target, allowed_roots):
    """Silinecek yol yalnızca izinli mod klasörlerinin DOĞRUDAN çocuğu olabilir."""
    target = Path(target)
    game_real = Path(os.path.realpath(game_root))
    parent_real = Path(os.path.realpath(target.parent))
    if not is_game_dir(game_root):
        raise SafetyError("oyun klasörü doğrulanamadı")
    if not is_inside(parent_real, game_real):
        raise SafetyError("oyun klasörünün dışında")
    if _n(parent_real) not in {_n(os.path.realpath(r)) for r in allowed_roots}:
        raise SafetyError("izinli mod klasörlerinin doğrudan içinde değil")
    if target.name in ("", ".", ".."):
        raise SafetyError("geçersiz ad")
    rel = Path(os.path.relpath(parent_real / target.name, game_real)).as_posix().lower()
    for pref in PROTECTED_PREFIXES:
        if rel == pref or rel.startswith(pref + "/"):
            raise SafetyError("korumalı oyun dosyası (%s)" % pref)
    if target.name.lower() == "cyberpunk2077.exe":
        raise SafetyError("oyunun ana dosyası")
    if not os.path.lexists(target):
        raise SafetyError("yol artık mevcut değil")


def game_running():
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Cyberpunk2077.exe"], capture_output=True,
                                 stdin=subprocess.DEVNULL, timeout=30, encoding="utf-8", errors="replace",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return "cyberpunk2077.exe" in out.stdout.lower()
        out = subprocess.run(["pgrep", "-fi", "Cyberpunk2077.exe"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=30)
        return out.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------------------
# Silme + geçmiş
# --------------------------------------------------------------------------------------
def _rm_err(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_path(p):
    p = Path(_lp(p))
    if is_link(p):
        try:
            os.unlink(p)
        except OSError:
            os.rmdir(p)
    elif p.is_dir():
        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=_rm_err)
        else:
            shutil.rmtree(p, onerror=_rm_err)
    else:
        try:
            os.unlink(p)
        except PermissionError:
            os.chmod(p, stat.S_IWRITE)
            os.unlink(p)


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_history():
    h = _load_json(HISTORY_JSON, [])
    return h if isinstance(h, list) else []


def _append_text_log(batch):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_TXT, "a", encoding="utf-8") as f:
            f.write("=== %s | %s | %s ===\n" % (batch["time"], "YEDEĞE TAŞINDI" if batch["mode"].startswith("yedek")
                                                 else "KALICI SİLİNDİ", batch["game_root"]))
            for it in batch["items"]:
                f.write("  - %s  [%s]  %s\n" % (it["name"], it["status"], "; ".join(it["labels"])))
            f.write("\n")
    except OSError:
        pass


def delete_mods(game_root, items, permanent=False, log=None):
    """Seçilen modları siler. Önce HER ŞEYİ doğrular; doğrulanamayan mod atlanır. Sonuç listesini döndürür."""
    say = log or (lambda s: None)
    game_root = Path(game_root)
    if not is_game_dir(game_root):
        raise SafetyError("Oyun klasörü doğrulanamadı.")
    if game_running():
        raise SafetyError("Cyberpunk 2077 çalışıyor. Önce oyunu kapat.")
    allowed = [ci_path(game_root, rel) for rel, _l, _m in SCAN_ROOTS]
    allowed = [a for a in allowed if a is not None]

    ok_items, results = [], []
    for it in items:
        if it.framework:
            results.append(_result(it, game_root, "atlandı", "altyapı bileşeni korunuyor"))
            continue
        try:
            for p in it.parts:
                validate_target(game_root, p.path, allowed)
            ok_items.append(it)
        except SafetyError as e:
            results.append(_result(it, game_root, "atlandı", "güvenlik: %s" % e))

    batch_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if not permanent and ok_items:
        if is_inside(BACKUP_DIR, game_root):
            raise SafetyError("Yedek klasörü oyun klasörünün içinde olamaz. CP2077_MODSILICI_DATA ayarını değiştir.")
        need = sum(i.size for i in ok_items)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(BACKUP_DIR).free < need * 1.1 + 50 * 1024 * 1024:
            raise SafetyError("Yedek için yeterli boş alan yok (%s gerekli). 'Kalıcı sil' seçeneğini kullanabilirsin."
                              % fmt_size(need))

    for it in ok_items:
        res = _result(it, game_root, "silindi", "")
        res["parts"] = []
        failed = 0
        for p in it.parts:
            rel = Path(os.path.relpath(p.path, game_root)).as_posix()
            part = {"root_rel": p.root_rel, "name": p.path.name, "rel": rel, "backup": None, "status": "silindi"}
            try:
                if permanent or is_link(p.path):
                    if is_link(p.path):
                        part["note"] = "bağlantı kaldırıldı (hedef içerik silinmedi)"
                    _remove_path(p.path)
                else:
                    dst = _unique(BACKUP_DIR / batch_id / Path(*rel.split("/")))
                    os.makedirs(_lp(dst.parent), exist_ok=True)
                    try:
                        shutil.move(_lp(p.path), _lp(dst))
                    except Exception:
                        if dst.exists():
                            try:
                                _remove_path(dst)
                            except Exception:
                                pass
                        raise
                    part["backup"] = str(dst)
                say("  ✓ %s" % rel)
            except Exception as e:  # noqa
                part["status"] = "hata: %s" % e
                failed += 1
                say("  ✗ %s -> %s" % (rel, e))
            res["parts"].append(part)
        if failed == len(it.parts):
            res["status"] = "hata"
        elif failed:
            res["status"] = "kısmen silindi"
        results.append(res)

    if results:
        batch = {"id": batch_id, "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "game_root": str(game_root), "mode": "kalici" if permanent else "yedek", "items": results}
        hist = load_history()
        hist.append(batch)
        try:
            _save_json(HISTORY_JSON, hist)
        except OSError as e:
            say("Geçmiş dosyası yazılamadı: %s" % e)
        _append_text_log(batch)
    return results


def _result(it, game_root, status, reason):
    return {"name": it.name, "labels": it.labels, "files": it.files, "size": it.size,
            "paths": it.rel_paths(game_root), "status": status, "reason": reason, "parts": []}


# ========================================================================================
# GELİŞMİŞ: TEMEL KARŞILAŞTIRMA (BASELINE) — kurulumdan sonra eklenen HER ŞEYİ bulur
# ========================================================================================
# Fikir: program oyunun ana klasörüne konur, "Temel Oluştur" ile o anki tüm dosya listesi
# (yol + boyut + değişim zamanı) kaydedilir. Sonradan "Karşılaştır" çalıştırıldığında, o
# temelde OLMAYAN her dosya/klasör "sonradan eklenmiş" sayılır ve silinebilir. Bilinen mod
# klasörleriyle sınırlı değildir; oyunun içine bırakılan HER ŞEYİ yakalar.
#
# ÖNEMLİ SINIRLAR (kullanıcıya da anlatılır):
#  - Temel, MOD KURMADAN ÖNCE (tercihen "dosya bütünlüğünü doğrula" sonrası) alınmalıdır.
#    Temel alındıktan SONRA kurulan modlar "sonradan eklenmiş" sayılır; temelden ÖNCE zaten
#    kurulu olan modlar özgünmüş gibi görünür ve yakalanmaz.
#  - Oyun güncellenirse (yama) yeni/değişen resmi dosyalar da "sonradan eklenmiş" gibi
#    görünebilir. Bu yüzden ana .exe dosyasının boyut/tarihi karşılaştırılır; uyuşmazsa
#    büyük bir uyarı gösterilir ve kullanıcı açıkça onaylamadan silme yapılmaz.
#  - r6/cache (oyunun kendi derlenmiş betik önbelleği) ve ekran görüntüsü klasörleri her
#    zaman göz ardı edilir; bunlar normal oynanışta da değişir/oluşur, mod sayılmaz.
BASELINE_ALWAYS_IGNORE_DIR = "r6/cache"
SELF_FILE_NAMES = {"cp2077_mod_silici.py", "cp2077_mod_silici.pyw", "calistir.bat", "__pycache__"}


def _is_self_name(name_lower):
    return name_lower in SELF_FILE_NAMES or name_lower.startswith("cp2077_mod_silici")


def _baseline_skip(rel_lower_parts, name_lower):
    if rel_lower_parts[:2] == ["r6", "cache"]:
        return True
    if any(("screenshot" in p) for p in rel_lower_parts):
        return True
    if _is_self_name(name_lower):
        return True
    return False


def _exe_fingerprint(game_root):
    exe = ci_path(game_root, "bin/x64/Cyberpunk2077.exe")
    if exe and exe.exists():
        st = exe.stat()
        return [st.st_size, int(st.st_mtime)]
    return None


def _walk_for_baseline(game_root, on_dir=None, on_file=None, errors=None, self_hits=None):
    """errors: okunamayan yolların ekleneceği liste (verilirse sessizce atlanmaz)."""
    base = _lp(game_root)

    def _err(e):
        if errors is not None:
            errors.append(str(getattr(e, "filename", None) or e))

    for r, dnames, fnames in os.walk(base, followlinks=False, onerror=_err):
        rel_r = r[len(base):].replace("\\", "/").strip("/")
        keep = []
        for d in dnames:
            rp = (rel_r + "/" + d) if rel_r else d
            parts = rp.lower().split("/")
            if _baseline_skip(parts, d.lower()) or is_link(Path(r) / d):
                continue
            keep.append(d)
            if on_dir:
                on_dir(rp)
        dnames[:] = keep
        for f in fnames:
            rp = (rel_r + "/" + f) if rel_r else f
            parts = rp.lower().split("/")
            if _baseline_skip(parts, f.lower()):
                if self_hits is not None and _is_self_name(f.lower()) and parts[:2] != ["r6", "cache"]:
                    self_hits.append(rp)
                continue
            fp = Path(r) / f
            if is_link(fp):
                continue
            try:
                st = fp.stat()
            except OSError as e:
                _err(e)
                continue
            if on_file:
                on_file(rp, st.st_size, int(st.st_mtime))


def baseline_path_for(game_root):
    key = re.sub(r"[^a-zA-Z0-9]+", "_", str(Path(os.path.realpath(game_root)))).strip("_")[-100:]
    return DATA_DIR / ("temel_%s.json" % key)


def build_baseline(game_root, progress_cb=None):
    game_root = Path(game_root)
    if not is_game_dir(game_root):
        raise SafetyError("Oyun klasörü doğrulanamadı.")
    files, dirs = {}, []
    count = [0]

    def on_dir(rp):
        dirs.append(rp)

    def on_file(rp, size, mtime):
        files[rp] = [size, mtime]
        count[0] += 1
        if progress_cb and count[0] % 1000 == 0:
            progress_cb(count[0])

    errs = []
    _walk_for_baseline(game_root, on_dir, on_file, errs)
    if errs:
        raise SafetyError("Temel oluşturulamadı: %d dosya/klasör okunamadı (ör. %s). Eksik bir temel, özgün "
                          "dosyaların silinmesine yol açabilir. Antivirüs/Steam'i kapatıp tekrar dene; gerekirse "
                          "programı yönetici olarak çalıştır." % (len(errs), errs[0]))
    data = {"version": 1, "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "game_root": str(game_root), "exe_fingerprint": _exe_fingerprint(game_root),
            "file_count": len(files), "dirs": sorted(dirs), "files": files}
    return data


def save_baseline(game_root, data):
    p = baseline_path_for(game_root)
    _save_json(p, data)
    return p


def load_baseline(game_root):
    p = baseline_path_for(game_root)
    if not p.exists():
        return None
    d = _load_json(p, None)
    return d if isinstance(d, dict) and "files" in d else None


class BaselineItem:
    """Temelde bulunmayan, sonradan eklenmiş tek bir öğe (dosya ya da tüm bir klasör)."""
    def __init__(self, key):
        self.key = key            # oyun köküne göre yol (örn. "archive/pc/mod/Yeni.archive")
        self.name = key
        self.rels = []
        self.files = 0
        self.size = 0
        self.has_self = False     # içinde programın bir kopyası var: silinmez


def diff_baseline(game_root, baseline):
    game_root = Path(game_root)
    if not is_game_dir(game_root):
        raise SafetyError("Oyun klasörü doğrulanamadı.")
    base_files = baseline.get("files", {})
    base_paths = set(base_files.keys()) | set(baseline.get("dirs", []))

    current = {}
    current_dirs = []

    def on_file(rp, size, mtime):
        current[rp] = [size, mtime]

    walk_errors = []
    self_hits = []
    _walk_for_baseline(game_root, current_dirs.append, on_file, walk_errors, self_hits)

    new_files = [p for p in current if p not in base_files]
    new_dirs = [d for d in current_dirs if d not in base_paths]
    changed_files = [p for p in current if p in base_files and tuple(current[p]) != tuple(base_files[p])]
    missing_files = [p for p in base_files if p not in current]

    def ancestor(relpath):
        parts = relpath.split("/")
        cur = ""
        for i, part in enumerate(parts):
            cur = part if i == 0 else cur + "/" + part
            if cur not in base_paths:
                return cur
        return relpath

    groups = {}
    for rp in new_files:
        anc = ancestor(rp)
        it = groups.setdefault(anc, BaselineItem(anc))
        it.rels.append(rp)
        it.files += 1
        it.size += current[rp][0]
    # İçinde dosya olmayan, sonradan oluşturulmuş boş klasörleri de yakala
    for dp in new_dirs:
        anc = ancestor(dp)
        if anc not in groups:
            groups[anc] = BaselineItem(anc)

    for it in groups.values():
        if any(h.startswith(it.key + "/") for h in self_hits):
            it.has_self = True

    exe_fp = _exe_fingerprint(game_root)
    version_mismatch = bool(baseline.get("exe_fingerprint")) and exe_fp != baseline.get("exe_fingerprint")

    return {"items": sorted(groups.values(), key=lambda x: x.name.lower()),
            "changed_files": changed_files, "missing_files": missing_files,
            "version_mismatch": version_mismatch, "new_file_count": len(new_files),
            "walk_errors": walk_errors,
            "baseline_created": baseline.get("created"), "baseline_file_count": len(base_files)}


def validate_baseline_target(game_root, rel, baseline, allow_protected=False):
    game_root = Path(game_root)
    if rel in baseline.get("files", {}) or rel in set(baseline.get("dirs", [])):
        raise SafetyError("bu, temelde kayıtlı özgün bir dosya/klasör")
    target = game_root / Path(*rel.split("/"))
    game_real = Path(os.path.realpath(game_root))
    if not is_inside(Path(os.path.realpath(target.parent)), game_real):
        raise SafetyError("oyun klasörünün dışında")
    low = rel.lower()
    if ".." in rel.split("/"):
        raise SafetyError("geçersiz yol")
    if not allow_protected:
        for pref in PROTECTED_PREFIXES:
            if low == pref or low.startswith(pref + "/"):
                raise SafetyError("korumalı oyun dosyası (%s)" % pref)
    if rel.lower() in ("bin/x64/cyberpunk2077.exe",):
        raise SafetyError("oyunun ana dosyası")
    try:
        me = os.path.realpath(__file__)
        if is_inside(me, os.path.realpath(target)):
            raise SafetyError("programın kendi dosyası/klasörü")
    except NameError:
        pass
    if not os.path.lexists(_lp(target)):
        raise SafetyError("yol artık mevcut değil")
    return target


def delete_baseline_items(game_root, baseline, items, permanent=False, log=None, allow_protected=False):
    say = log or (lambda s: None)
    game_root = Path(game_root)
    if not is_game_dir(game_root):
        raise SafetyError("Oyun klasörü doğrulanamadı.")
    if game_running():
        raise SafetyError("Cyberpunk 2077 çalışıyor. Önce oyunu kapat.")

    ok, results = [], []
    for it in items:
        try:
            if getattr(it, "has_self", False):
                raise SafetyError("içinde programın bir kopyası var; programı oyun klasörünün dışına taşıyıp tekrar dene")
            target = validate_baseline_target(game_root, it.key, baseline, allow_protected)
            ok.append((it, target))
        except SafetyError as e:
            results.append({"name": it.name, "rel": it.key, "status": "atlandı", "reason": str(e),
                            "files": it.files, "size": it.size, "backup": None})

    batch_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if not permanent and ok:
        if is_inside(BACKUP_DIR, game_root):
            raise SafetyError("Yedek klasörü oyun klasörünün içinde olamaz.")
        need = sum(i.size for i, _t in ok)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(BACKUP_DIR).free < need * 1.1 + 50 * 1024 * 1024:
            raise SafetyError("Yedek için yeterli boş alan yok (%s gerekli)." % fmt_size(need))

    for it, target in ok:
        entry = {"name": it.name, "rel": it.key, "status": "silindi", "reason": "",
                 "files": it.files, "size": it.size, "backup": None}
        try:
            if permanent or is_link(target):
                _remove_path(target)
            else:
                dst = _unique(BACKUP_DIR / batch_id / Path(*it.key.split("/")))
                os.makedirs(_lp(dst.parent), exist_ok=True)
                shutil.move(_lp(target), _lp(dst))
                entry["backup"] = str(dst)
            say("  ✓ %s" % it.key)
        except Exception as e:  # noqa
            entry["status"] = "hata"
            entry["reason"] = str(e)
            say("  ✗ %s -> %s" % (it.key, e))
        results.append(entry)

    if results:
        batch = {"id": batch_id, "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "game_root": str(game_root), "mode": "yedek_temel" if not permanent else "kalici_temel",
                 "items": [{"name": r["name"], "labels": ["Tam Karşılaştırma (Temel)"], "status": r["status"],
                           "reason": r.get("reason", ""),
                           "parts": [{"root_rel": "/".join(r["rel"].split("/")[:-1]),
                                     "name": r["rel"].split("/")[-1], "rel": r["rel"],
                                     "backup": r.get("backup"), "status": r["status"]}]} for r in results]}
        hist = load_history()
        hist.append(batch)
        try:
            _save_json(HISTORY_JSON, hist)
        except OSError as e:
            say("Geçmiş dosyası yazılamadı: %s" % e)
        _append_text_log(batch)
    return results


def reset_to_pristine(game_root, baseline, permanent=False, log=None):
    """Temelde olmayan HER ŞEYİ siler, sonra tekrar tarayıp gerçekten temiz olduğunu doğrular.
    Dönen sözlük: removed, failed, leftovers (doğrulamada hâlâ duran yeni öğeler),
    changed_files, missing_files, clean (bool)."""
    say = log or (lambda s: None)
    game_root = Path(game_root)
    d = diff_baseline(game_root, baseline)
    if d["version_mismatch"]:
        raise SafetyError("Oyunun .exe dosyası temelden beri değişmiş (oyun güncellenmiş olabilir). Tam sıfırlama, "
                          "yeni resmi dosyaları da silebileceği için engellendi. Önce oyunu Steam/GOG/Epic'ten "
                          "'dosya bütünlüğünü doğrula' ile onar, sonra temeli yeniden oluştur.")
    if d["walk_errors"]:
        raise SafetyError("%d dosya/klasör okunamadı (ör. %s). Tarama eksik olduğu için tam sıfırlama güvenlik "
                          "amacıyla durduruldu. Antivirüs/Steam'i kapatıp tekrar dene; gerekirse programı "
                          "yönetici olarak çalıştır." % (len(d["walk_errors"]), d["walk_errors"][0]))
    selfy = [i.key for i in d["items"] if i.has_self]
    if selfy:
        raise SafetyError("Şu klasör(ler)in içinde programın bir kopyası var: %s. Tam sıfırlama programı da "
                          "silerdi. Programı oyun klasörünün dışına (ör. Masaüstü) taşıyıp tekrar dene."
                          % ", ".join(selfy[:5]))
    removed = 0
    out = delete_baseline_items(game_root, baseline, d["items"], permanent, log=say, allow_protected=True)
    removed += sum(1 for r in out if r["status"] == "silindi")
    # DOĞRULAMA: yeniden tara, kalan varsa bir kez daha dene
    for attempt in range(2):
        d2 = diff_baseline(game_root, baseline)
        if not d2["items"]:
            break
        say("Doğrulama: %d öğe hâlâ duruyor, tekrar deneniyor..." % len(d2["items"]))
        out = delete_baseline_items(game_root, baseline, d2["items"], permanent, log=say, allow_protected=True)
        removed += sum(1 for r in out if r["status"] == "silindi")
    final = diff_baseline(game_root, baseline)
    leftovers = [it.key for it in final["items"]]
    unreadable = final["walk_errors"]
    return {"removed": removed, "leftovers": leftovers, "unreadable": unreadable,
            "changed_files": final["changed_files"], "missing_files": final["missing_files"],
            "clean": not leftovers and not unreadable}


def restore_item(batch, item):
    """Yedeğe taşınmış bir modu eski yerine geri koyar. (mesajlar listesi) döndürür."""
    msgs = []
    game_root = Path(batch["game_root"])
    if not is_game_dir(game_root):
        return ["Oyun klasörü bulunamadı: %s" % game_root]
    if batch.get("mode") not in ("yedek", "yedek_temel"):
        return ["Bu öğe kalıcı silinmiş; geri yüklenemez."]
    known = {rel for rel, _l, _m in SCAN_ROOTS} | {""}
    for part in item.get("parts", []):
        bk = part.get("backup")
        if not bk or not os.path.lexists(_lp(bk)):
            msgs.append("Yedek yok/bulunamadı: %s" % part.get("rel"))
            continue
        rr = part.get("root_rel", "")
        if batch.get("mode") == "yedek_temel":
            if ".." in rr.split("/"):
                msgs.append("Geçersiz hedef klasör, atlandı: %s" % part.get("rel"))
                continue
        elif rr not in known:
            msgs.append("Tanınmayan hedef klasör, atlandı: %s" % part.get("rel"))
            continue
        if part["root_rel"] == "":
            parent = game_root
        else:
            parent = ci_path(game_root, part["root_rel"]) or (game_root / Path(*part["root_rel"].split("/")))
        dest = parent / part["name"]
        if os.path.lexists(_lp(dest)):
            msgs.append("Hedefte zaten var, atlandı: %s" % part["rel"])
            continue
        try:
            os.makedirs(_lp(parent), exist_ok=True)
            shutil.move(_lp(bk), _lp(dest))
            part["backup"] = None
            part["status"] = "geri yüklendi"
            msgs.append("Geri yüklendi: %s" % part["rel"])
        except Exception as e:  # noqa
            msgs.append("Hata (%s): %s" % (part["rel"], e))
    if all(p.get("status") == "geri yüklendi" for p in item.get("parts", [])) and item.get("parts"):
        item["status"] = "geri yüklendi"
    return msgs


def save_history(hist):
    _save_json(HISTORY_JSON, hist)


def load_config():
    c = _load_json(CONFIG_JSON, {})
    return c if isinstance(c, dict) else {}


def save_config(c):
    try:
        _save_json(CONFIG_JSON, c)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# Metin modu
# --------------------------------------------------------------------------------------
def parse_selection(text, n):
    out = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(i for i in out if 1 <= i <= n)


def print_scan(res):
    print("\nOyun klasörü: %s" % res.game_root)
    print("Taranan konumlar: %s" % (", ".join(r for r, _p in res.roots) or "hiçbiri"))
    for w in res.warnings:
        print("! " + w)
    print("\nBulunan mod sayısı: %d   (%d dosya, %s)" % (len(res.mods), res.total_files, fmt_size(res.total_size)))
    print("Korunan altyapı bileşeni: %d (silinmez)\n" % len(res.frameworks))
    for i, m in enumerate(res.mods, 1):
        print("%3d) %-40s %-32s %5d dosya  %9s" % (i, m.name[:40], "/".join(m.labels)[:32], m.files, fmt_size(m.size)))
    if res.frameworks:
        print("\nAltyapı (korunur): " + ", ".join(f.name for f in res.frameworks))


def show_history_cli():
    hist = load_history()
    if not hist:
        print("Geçmiş boş.")
        return
    for b in hist:
        print("\n%s  [%s]" % (b["time"], "yedeğe taşındı" if b["mode"].startswith("yedek") else "kalıcı silindi"))
        for it in b["items"]:
            print("   - %s (%s) %s" % (it["name"], it["status"], it.get("reason", "")))


def cli_main(game_root):
    permanent = False
    res = scan(game_root)
    print_scan(res)
    while True:
        print("\nKomutlar: sil 1,3,5-7 | hepsi | kalici (aç/kapat, şu an: %s) | gecmis | yenile | cikis"
              % ("KALICI SİL" if permanent else "yedeğe taşı"))
        cmd = input("> ").strip().lower()
        if cmd in ("cikis", "çıkış", "q", "exit"):
            return
        if cmd == "yenile":
            res = scan(game_root)
            print_scan(res)
        elif cmd == "gecmis" or cmd == "geçmiş":
            show_history_cli()
        elif cmd == "kalici" or cmd == "kalıcı":
            permanent = not permanent
        elif cmd.startswith("sil") or cmd == "hepsi":
            idx = list(range(1, len(res.mods) + 1)) if cmd == "hepsi" else parse_selection(cmd[3:], len(res.mods))
            if not idx:
                print("Geçerli numara girilmedi.")
                continue
            chosen = [res.mods[i - 1] for i in idx]
            print("\nSilinecekler (%s):" % ("KALICI" if permanent else "yedeğe taşınacak"))
            for m in chosen:
                print("  - %s  (%s)" % (m.name, ", ".join(m.rel_paths(res.game_root))))
            if input("Onaylamak için EVET yaz: ").strip() != "EVET":
                print("İptal edildi.")
                continue
            try:
                out = delete_mods(game_root, chosen, permanent, log=print)
                for r in out:
                    print("%s: %s %s" % (r["name"], r["status"], r.get("reason", "")))
            except SafetyError as e:
                print("DURDURULDU: %s" % e)
            res = scan(game_root)
            print_scan(res)


# --------------------------------------------------------------------------------------
# Arayüz (tkinter) — Cyberpunk teması
# --------------------------------------------------------------------------------------
THEME = {
    "bg": "#0a0a0f",       # ana zemin
    "panel": "#13131b",    # kart / tablo zemini
    "panel2": "#1c1c28",   # düğme / başlık zemini
    "line": "#34344a",     # ince çizgiler
    "text": "#f0f0f7",     # ana yazı (koyu zeminde çok yüksek kontrast)
    "muted": "#aeaec4",    # ikincil yazı (yine okunaklı)
    "yellow": "#fcee0a",   # Cyberpunk sarısı
    "cyan": "#00f0ff",
    "red": "#ff3860",
    "green": "#39ff88",
    "orange": "#ffb020",
    "sel": "#0f4552",      # seçili satır
}
FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"
CHK_ON, CHK_OFF = "[X]", "[ ]"


def log_error(text):
    """.pyw'de konsol olmadığı için hatalar buraya yazılır: ~/CP2077_ModSilici/hata.log"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "hata.log", "a", encoding="utf-8") as f:
            f.write("=== %s ===\n%s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass


def gui_main(start_path=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog

    T = THEME
    try:  # Windows'ta keskin yazı (yüksek DPI)
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("%s  v%s" % (APP_NAME, VERSION))
            self.configure(bg=T["bg"])
            self.scale = max(1.0, self.winfo_fpixels("1i") / 96.0)
            w = min(self.S(1080), self.winfo_screenwidth() - 60)
            h = min(self.S(820), self.winfo_screenheight() - 90)
            self.geometry("%dx%d+30+20" % (w, h))
            self.minsize(min(self.S(940), w), min(self.S(720), h))

            self.cfg = load_config()
            self.res = None
            self.checked = set()
            self.bl = None
            self.bl_diff = None
            self.bl_checked = set()
            self.hist = []
            self.hist_index = {}
            self.path_var = tk.StringVar(value=start_path or self.cfg.get("game_path", ""))
            self.perm_var = tk.BooleanVar(value=False)
            self.fw_var = tk.BooleanVar(value=False)
            self.bl_perm = tk.BooleanVar(value=False)
            self.bl_override = tk.BooleanVar(value=False)
            self._style()
            self._build()
            self.after(200, self._first_run)

        def S(self, n):
            return int(n * getattr(self, "scale", 1.0))

        def report_callback_exception(self, exc, val, tb):
            import traceback
            log_error("".join(traceback.format_exception(exc, val, tb)))
            self.config(cursor="")
            messagebox.showerror(APP_NAME, "Beklenmeyen bir hata oluştu:\n\n%s\n\nAyrıntı: %s"
                                 % (val, DATA_DIR / "hata.log"))

        # ------------------------------------------------------------------ stil
        def _style(self):
            st = ttk.Style(self)
            st.theme_use("clam")
            st.configure(".", background=T["bg"], foreground=T["text"], font=(FONT_UI, 10),
                         bordercolor=T["line"], lightcolor=T["bg"], darkcolor=T["bg"],
                         troughcolor=T["panel"], focuscolor=T["bg"])
            st.configure("Tab.TFrame", background=T["bg"])
            st.configure("Card.TFrame", background=T["panel"])

            st.configure("TLabel", background=T["bg"], foreground=T["text"], font=(FONT_UI, 10))
            st.configure("Muted.TLabel", foreground=T["muted"], font=(FONT_UI, 9, "bold"))
            st.configure("Info.TLabel", foreground=T["cyan"], font=(FONT_UI, 11, "bold"))
            st.configure("Warn.TLabel", foreground=T["orange"], font=(FONT_UI, 10))
            st.configure("Intro.TLabel", foreground=T["muted"], font=(FONT_UI, 10))
            st.configure("Card.TLabel", background=T["panel"], foreground=T["text"], font=(FONT_UI, 10))
            st.configure("CardMuted.TLabel", background=T["panel"], foreground=T["muted"], font=(FONT_UI, 10))
            st.configure("CardTitle.TLabel", background=T["panel"], foreground=T["yellow"],
                         font=(FONT_UI, 11, "bold"))
            st.configure("DangerTitle.TLabel", background=T["panel"], foreground=T["red"],
                         font=(FONT_UI, 11, "bold"))

            def btn(name, bg, fg, hover, border=None):
                st.configure(name, background=bg, foreground=fg, bordercolor=border or bg,
                             lightcolor=bg, darkcolor=bg, borderwidth=1, relief="flat",
                             padding=(16, 8), font=(FONT_UI, 10, "bold"), focuscolor=bg)
                st.map(name, background=[("active", hover), ("disabled", T["panel2"])],
                       foreground=[("disabled", T["muted"])],
                       bordercolor=[("active", hover)])
            btn("Neon.TButton", T["yellow"], "#000000", "#fff75e")
            btn("Ghost.TButton", T["panel2"], T["cyan"], "#26304a", border=T["cyan"])
            btn("Danger.TButton", T["red"], "#000000", "#ff6b88")

            # clam teması: kutunun dolgusu = indicatorbackground, işaret rengi = indicatorforeground
            # (işaretli kutu sarı dolgu + siyah tik: koyu zeminde net görünür)
            cb = dict(indicatorbackground=T["panel2"], indicatorforeground="#000000",
                      upperbordercolor=T["muted"], lowerbordercolor=T["muted"],
                      indicatorsize=self.S(16), indicatormargin=(0, 2, 8, 2), font=(FONT_UI, 10))
            st.configure("TCheckbutton", background=T["bg"], foreground=T["text"], **cb)
            st.map("TCheckbutton",
                   indicatorbackground=[("selected", T["yellow"]), ("active", "#2c2c42")],
                   upperbordercolor=[("selected", T["yellow"])], lowerbordercolor=[("selected", T["yellow"])],
                   background=[("active", T["bg"])])
            st.configure("Card.TCheckbutton", background=T["panel"], foreground=T["text"], **cb)
            st.map("Card.TCheckbutton",
                   indicatorbackground=[("selected", T["yellow"]), ("active", "#2c2c42")],
                   upperbordercolor=[("selected", T["yellow"])], lowerbordercolor=[("selected", T["yellow"])],
                   background=[("active", T["panel"])])

            st.configure("TEntry", fieldbackground=T["panel2"], foreground=T["text"],
                         insertcolor=T["cyan"], bordercolor=T["line"], lightcolor=T["line"],
                         darkcolor=T["line"], padding=6)
            st.map("TEntry", bordercolor=[("focus", T["cyan"])], lightcolor=[("focus", T["cyan"])],
                   darkcolor=[("focus", T["cyan"])])

            st.configure("Treeview", background=T["panel"], fieldbackground=T["panel"],
                         foreground=T["text"], rowheight=self.S(30), borderwidth=0, font=(FONT_UI, 10))
            st.configure("Treeview.Heading", background=T["panel2"], foreground=T["yellow"],
                         font=(FONT_UI, 9, "bold"), relief="flat", padding=(8, 7), borderwidth=0)
            st.map("Treeview", background=[("selected", T["sel"])], foreground=[("selected", "#ffffff")])
            st.map("Treeview.Heading", background=[("active", "#2a2a3e")])
            st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

            st.configure("TNotebook", background=T["bg"], borderwidth=0, tabmargins=(0, 4, 0, 0))
            st.configure("TNotebook.Tab", background=T["panel2"], foreground=T["text"],
                         padding=(22, 10), font=(FONT_UI, 10, "bold"), borderwidth=0)
            st.map("TNotebook.Tab", background=[("selected", T["yellow"]), ("active", "#2c2c42")],
                   foreground=[("selected", "#000000")])

            st.configure("Vertical.TScrollbar", background=T["panel2"], troughcolor=T["panel"],
                         bordercolor=T["panel"], arrowcolor=T["cyan"], lightcolor=T["panel2"],
                         darkcolor=T["panel2"])
            st.map("Vertical.TScrollbar", background=[("active", "#34344a")])

        # ------------------------------------------------------------------ yardımcılar
        def mkbtn(self, parent, text, cmd, kind="ghost"):
            style = {"neon": "Neon.TButton", "ghost": "Ghost.TButton", "danger": "Danger.TButton"}[kind]
            return ttk.Button(parent, text=text, command=cmd, style=style, cursor="hand2")

        def _make_tree(self, parent, cols, height=8):
            tv = ttk.Treeview(parent, columns=[c[0] for c in cols], show="headings",
                              selectmode="browse", height=height)
            for c, t, w, a in cols:
                tv.heading(c, text=t, anchor=a)
                tv.column(c, width=self.S(w), anchor=a, stretch=(a == "w"))
            sb = ttk.Scrollbar(parent, orient="vertical", command=tv.yview)
            tv.configure(yscrollcommand=sb.set)
            tv.pack(side="left", fill="both", expand=True)
            sb.pack(side="left", fill="y")
            tv.tag_configure("fw", foreground=T["muted"])
            return tv

        def _card(self, parent, title, danger=False):
            color = T["red"] if danger else T["yellow"]
            outer = tk.Frame(parent, bg=T["panel"], highlightthickness=1, highlightbackground=T["line"])
            tk.Frame(outer, width=5, bg=color).pack(side="left", fill="y")
            inner = ttk.Frame(outer, style="Card.TFrame", padding=(14, 10, 14, 12))
            inner.pack(side="left", fill="both", expand=True)
            ttk.Label(inner, text=title, style="DangerTitle.TLabel" if danger else "CardTitle.TLabel"
                      ).pack(anchor="w", pady=(0, 6))
            return outer, inner

        def busy(self, text):
            self.status.config(text=text)
            self.config(cursor="watch")
            self.update()

        def idle(self, text="Hazır."):
            self.config(cursor="")
            self.status.config(text=text)

        def set_detail(self, text):
            self.detail.config(state="normal")
            self.detail.delete("1.0", "end")
            self.detail.insert("end", text)
            self.detail.config(state="disabled")

        def _text_box(self, parent, height):
            return tk.Text(parent, height=height, wrap="word", state="disabled", bg=T["panel"],
                           fg=T["text"], relief="flat", font=(FONT_MONO, 10), padx=12, pady=8,
                           highlightthickness=1, highlightbackground=T["line"])

        # ------------------------------------------------------------------ arayüz kurulumu
        def _build(self):
            head = tk.Frame(self, bg=T["panel"])
            head.pack(fill="x")
            tk.Frame(head, bg=T["yellow"], height=4).pack(fill="x")
            row = tk.Frame(head, bg=T["panel"])
            row.pack(fill="x", padx=18, pady=(10, 10))
            tk.Label(row, text="CYBERPUNK 2077", bg=T["panel"], fg=T["yellow"],
                     font=(FONT_UI, 20, "bold")).pack(side="left")
            tk.Label(row, text="   //   MOD BULUCU VE SİLİCİ", bg=T["panel"], fg=T["cyan"],
                     font=(FONT_UI, 13, "bold")).pack(side="left", pady=(7, 0))
            tk.Label(row, text="v%s" % VERSION, bg=T["panel"], fg=T["muted"],
                     font=(FONT_MONO, 10)).pack(side="right", pady=(7, 0))

            pr = tk.Frame(self, bg=T["bg"])
            pr.pack(fill="x", padx=18, pady=(12, 6))
            ttk.Label(pr, text="OYUN KLASÖRÜ", style="Muted.TLabel").pack(side="left")
            ttk.Entry(pr, textvariable=self.path_var, font=(FONT_MONO, 10)).pack(
                side="left", fill="x", expand=True, padx=10)
            self.mkbtn(pr, "Otomatik Bul", self.auto_find).pack(side="left", padx=3)
            self.mkbtn(pr, "Seç...", self.browse).pack(side="left", padx=3)
            self.mkbtn(pr, "Tara / Yenile", self.do_scan, "neon").pack(side="left", padx=(3, 0))

            self.status = tk.Label(self, text="Hazır.", anchor="w", bg=T["panel"], fg=T["text"],
                                   font=(FONT_MONO, 9), padx=16, pady=6)
            self.status.pack(side="bottom", fill="x")

            self.nb = ttk.Notebook(self)
            self.nb.pack(fill="both", expand=True, padx=18, pady=(4, 12))
            self._build_mods_tab()
            self._build_baseline_tab()
            self._build_history_tab()
            self.nb.bind("<<NotebookTabChanged>>", self.on_tab)

        # ---------- Sekme 1: Modlar
        def _build_mods_tab(self):
            t = ttk.Frame(self.nb, style="Tab.TFrame", padding=(0, 12, 0, 0))
            self.nb.add(t, text="  MODLAR  ")
            self.summary = ttk.Label(t, text="", style="Info.TLabel")
            self.summary.pack(fill="x")
            self.warn = ttk.Label(t, text="", style="Warn.TLabel", wraplength=self.S(1000), justify="left")
            self.warn.pack(fill="x", pady=(2, 6))

            mid = ttk.Frame(t, style="Tab.TFrame")
            mid.pack(fill="both", expand=True)
            self.tree = self._make_tree(mid, [("sel", "SEÇ", 60, "center"), ("name", "MOD ADI", 340, "w"),
                                              ("type", "TÜR", 280, "w"), ("files", "DOSYA", 80, "e"),
                                              ("size", "BOYUT", 100, "e")])
            self.tree.bind("<Button-1>", self.on_click)
            self.tree.bind("<space>", self.on_space)
            self.tree.bind("<<TreeviewSelect>>", self.on_select)

            self.detail = self._text_box(t, 5)
            self.detail.pack(fill="x", pady=(8, 8))

            opt = ttk.Frame(t, style="Tab.TFrame")
            opt.pack(fill="x", pady=(0, 8))
            ttk.Checkbutton(opt, text="Kalıcı sil (yedek alma, geri döndürülemez)",
                            variable=self.perm_var).pack(side="left")
            ttk.Checkbutton(opt, text="Altyapı bileşenlerini de göster (silinemez)", variable=self.fw_var,
                            command=self.refresh_tree).pack(side="left", padx=24)

            btn = ttk.Frame(t, style="Tab.TFrame")
            btn.pack(fill="x")
            self.mkbtn(btn, "Tümünü Seç", self.select_all).pack(side="left", padx=(0, 6))
            self.mkbtn(btn, "Seçimi Temizle", self.select_none).pack(side="left")
            self.mkbtn(btn, "Seçilenleri Sil", self.do_delete, "danger").pack(side="right")

        # ---------- Sekme 2: Tam karşılaştırma
        def _build_baseline_tab(self):
            t = ttk.Frame(self.nb, style="Tab.TFrame", padding=(0, 10, 0, 0))
            self.nb.add(t, text="  TAM KARŞILAŞTIRMA  ")
            ttk.Label(t, style="Intro.TLabel", wraplength=self.S(1000), justify="left",
                      text="Bilinen mod klasörleriyle sınırlı kalmaz: oyun klasörüne temelden SONRA eklenen HER ŞEYİ "
                           "bulur. Temeli mod kurmadan önce, temiz kurulumda almak en güvenlisidir."
                      ).pack(fill="x", pady=(0, 6))

            o1, c1 = self._card(t, "ADIM 1  —  TEMEL AL")
            o1.pack(fill="x", pady=4)
            r1 = ttk.Frame(c1, style="Card.TFrame")
            r1.pack(fill="x")
            self.mkbtn(r1, "Temel Oluştur / Güncelle", self.bl_create).pack(side="left")
            self.bl_status = ttk.Label(r1, text="", style="Card.TLabel")
            self.bl_status.pack(side="left", padx=16)

            # Adım 3 her zaman görünsün diye önce alta sabitlenir
            o3, c3 = self._card(t, "ADIM 3  —  TAM SIFIRLA (İLK HALE DÖNDÜR)", danger=True)
            o3.pack(side="bottom", fill="x", pady=4)

            o2, c2 = self._card(t, "ADIM 2  —  KARŞILAŞTIR VE SEÇEREK SİL")
            o2.pack(fill="both", expand=True, pady=4)
            r2 = ttk.Frame(c2, style="Card.TFrame")
            r2.pack(fill="x", pady=(0, 6))
            self.mkbtn(r2, "Şimdi Karşılaştır", self.bl_compare, "neon").pack(side="left")
            self.mkbtn(r2, "Tümünü Seç", self.bl_select_all).pack(side="left", padx=(10, 4))
            self.mkbtn(r2, "Temizle", self.bl_select_none).pack(side="left")
            self.bl_info = ttk.Label(r2, text="", style="CardMuted.TLabel")
            self.bl_info.pack(side="left", padx=14)
            wrap = ttk.Frame(c2, style="Card.TFrame")
            wrap.pack(fill="both", expand=True)
            self.bl_tree = self._make_tree(wrap, [("sel", "SEÇ", 60, "center"),
                                                  ("name", "SONRADAN EKLENMİŞ YOL", 560, "w"),
                                                  ("files", "DOSYA", 80, "e"), ("size", "BOYUT", 100, "e")],
                                           height=3)
            self.bl_tree.bind("<Button-1>", self.bl_on_click)
            self.bl_warn = ttk.Label(c2, text="", style="Card.TLabel", wraplength=self.S(960),
                                     justify="left", foreground=T["red"])
            self.bl_warn.pack(fill="x", pady=(6, 0))
            r3 = ttk.Frame(c2, style="Card.TFrame")
            r3.pack(fill="x", pady=(8, 0))
            ttk.Checkbutton(r3, text="Kalıcı sil (yedek alma)", variable=self.bl_perm,
                            style="Card.TCheckbutton").pack(side="left")
            self.bl_override_chk = ttk.Checkbutton(
                r3, text="Sürüm uyuşmazlığına rağmen devam et (riski biliyorum)",
                variable=self.bl_override, style="Card.TCheckbutton")
            self.mkbtn(r3, "İşaretlenenleri Sil", self.bl_delete, "danger").pack(side="right")

            r4 = ttk.Frame(c3, style="Card.TFrame")
            r4.pack(fill="x")
            ttk.Label(r4, style="Card.TLabel", wraplength=self.S(700), justify="left",
                      text="Temelde olmayan HER ŞEYİ siler (korumalı klasörler dahil) ve sonunda tekrar tarayıp "
                           "temiz olduğunu doğrular. Yukarıdaki 'Kalıcı sil' seçeneğine uyar."
                      ).pack(side="left", fill="x", expand=True)
            self.mkbtn(r4, "TAM SIFIRLA", self.bl_reset, "danger").pack(side="right", padx=(12, 0))

        # ---------- Sekme 3: Geçmiş
        def _build_history_tab(self):
            t = ttk.Frame(self.nb, style="Tab.TFrame", padding=(0, 12, 0, 0))
            self.nb.add(t, text="  GEÇMİŞ  ")
            wrap = ttk.Frame(t, style="Tab.TFrame")
            wrap.pack(fill="both", expand=True)
            self.hist_tree = self._make_tree(wrap, [("time", "TARİH", 170, "w"), ("name", "ÖĞE", 440, "w"),
                                                    ("status", "DURUM", 160, "w"), ("mode", "YÖNTEM", 160, "w")],
                                             height=12)
            ttk.Label(t, text="Geçmiş dosyası: %s" % HISTORY_TXT, style="Intro.TLabel").pack(
                anchor="w", pady=(8, 8))
            r = ttk.Frame(t, style="Tab.TFrame")
            r.pack(fill="x")
            self.mkbtn(r, "Yenile", self.refresh_history).pack(side="left")
            self.mkbtn(r, "Seçili Öğeyi Geri Yükle", self.restore_selected, "neon").pack(side="right")

        def on_tab(self, _ev=None):
            idx = self.nb.index(self.nb.select())
            if idx == 1:
                self.bl_refresh_status()
            elif idx == 2:
                self.refresh_history()

        def _first_run(self):
            if not self.path_var.get() or not is_game_dir(self.path_var.get()):
                self.auto_find(silent=True)
            if is_game_dir(self.path_var.get()):
                self.do_scan()

        # ------------------------------------------------------------------ oyun klasörü
        def auto_find(self, silent=False):
            found = find_game_dirs()
            if not found:
                if not silent:
                    messagebox.showinfo(APP_NAME, "Oyun klasörü otomatik bulunamadı. 'Seç...' ile elle göster.")
                return
            if len(found) == 1:
                self.path_var.set(str(found[0]))
            else:
                self.path_var.set(self._choose([str(f) for f in found]) or str(found[0]))
            self.do_scan()

        def _choose(self, options):
            win = tk.Toplevel(self)
            win.title("Oyun klasörünü seç")
            win.configure(bg=T["bg"], padx=16, pady=12)
            win.grab_set()
            var = tk.StringVar(value=options[0])
            for o in options:
                tk.Radiobutton(win, text=o, variable=var, value=o, bg=T["bg"], fg=T["text"],
                               selectcolor=T["panel2"], activebackground=T["bg"], activeforeground=T["cyan"],
                               font=(FONT_UI, 10)).pack(anchor="w", pady=3)
            self.mkbtn(win, "Tamam", win.destroy, "neon").pack(pady=(10, 0))
            self.wait_window(win)
            return var.get()

        def browse(self):
            p = filedialog.askdirectory(title="Cyberpunk 2077 oyun klasörünü seç")
            if p:
                self.path_var.set(p)
                self.do_scan()

        def cur_root(self):
            p = self.path_var.get().strip().strip('"')
            if not is_game_dir(p):
                messagebox.showerror(APP_NAME, "Önce geçerli bir Cyberpunk 2077 oyun klasörü seç.")
                return None
            return Path(p)

        # ------------------------------------------------------------------ MODLAR sekmesi
        def do_scan(self):
            p = self.path_var.get().strip().strip('"')
            try:
                self.busy("Taranıyor...")
                self.res = scan(p)
            except SafetyError as e:
                self.res = None
                self.refresh_tree()
                self.idle("Tarama başarısız.")
                messagebox.showerror(APP_NAME, str(e))
                return
            self.cfg["game_path"] = p
            save_config(self.cfg)
            self.checked &= {m.key for m in self.res.mods}
            self.refresh_tree()
            self.bl_refresh_status()
            self.idle("Tarama tamamlandı.")

        def _summary_text(self):
            r = self.res
            return ("Bulunan mod: %d    •    %d dosya, %s    •    Korunan altyapı: %d    •    Seçili: %d"
                    % (len(r.mods), r.total_files, fmt_size(r.total_size), len(r.frameworks), len(self.checked)))

        def refresh_tree(self):
            self.tree.delete(*self.tree.get_children())
            r = self.res
            if not r:
                self.summary.config(text="")
                self.warn.config(text="")
                return
            for m in r.mods:
                self.tree.insert("", "end", iid=m.key,
                                 values=(CHK_ON if m.key in self.checked else CHK_OFF, m.name,
                                         " + ".join(m.labels), m.files, fmt_size(m.size)))
            if self.fw_var.get():
                for f in r.frameworks:
                    self.tree.insert("", "end", iid="fw:" + f.key, tags=("fw",),
                                     values=("KİLİT", f.name, "Altyapı — korunuyor", f.files, fmt_size(f.size)))
            self.summary.config(text=self._summary_text())
            self.warn.config(text="  ".join(r.warnings))
            if not r.mods:
                self.set_detail("Bilinen mod klasörlerinde silinebilir mod bulunamadı.\n"
                                "Başka yerlere eklenmiş dosyaları bulmak için TAM KARŞILAŞTIRMA sekmesini kullan.")
            else:
                self.set_detail("Bir satıra tıklayarak seç / seçimi kaldır. Ayrıntılar burada görünür.")

        def toggle(self, iid):
            if iid.startswith("fw:") or not self.tree.exists(iid):
                return
            if iid in self.checked:
                self.checked.discard(iid)
            else:
                self.checked.add(iid)
            self.tree.set(iid, "sel", CHK_ON if iid in self.checked else CHK_OFF)
            if self.res:
                self.summary.config(text=self._summary_text())

        def on_click(self, ev):
            if self.tree.identify_region(ev.x, ev.y) == "cell":
                iid = self.tree.identify_row(ev.y)
                if iid:
                    self.tree.selection_set(iid)
                    self.toggle(iid)
                    return "break"

        def on_space(self, _ev):
            sel = self.tree.selection()
            if sel:
                self.toggle(sel[0])
            return "break"

        def select_all(self):
            if self.res:
                self.checked = {m.key for m in self.res.mods}
                self.refresh_tree()

        def select_none(self):
            self.checked = set()
            self.refresh_tree()

        def on_select(self, _ev):
            sel = self.tree.selection()
            if not sel or not self.res:
                return
            pool = {("fw:" + f.key): f for f in self.res.frameworks}
            pool.update({m.key: m for m in self.res.mods})
            m = pool.get(sel[0])
            if m:
                self.set_detail("%s\n%s" % (m.name, "\n".join("• " + p for p in m.rel_paths(self.res.game_root))))

        def do_delete(self):
            if not self.res:
                messagebox.showinfo(APP_NAME, "Önce oyun klasörünü tara.")
                return
            chosen = [m for m in self.res.mods if m.key in self.checked]
            if not chosen:
                messagebox.showinfo(APP_NAME, "Önce silinecek modları işaretle (satıra tıkla).")
                return
            perm = self.perm_var.get()
            names = "\n".join("• " + m.name for m in chosen[:15]) + (
                "\n… ve %d mod daha" % (len(chosen) - 15) if len(chosen) > 15 else "")
            total = sum(m.size for m in chosen)
            msg = "%d mod %s:\n\n%s\n\nToplam: %s\n\nDevam edilsin mi?" % (
                len(chosen), "KALICI OLARAK SİLİNECEK (geri dönüş yok)" if perm
                else "yedek klasörüne taşınacak (geri yüklenebilir)", names, fmt_size(total))
            if not messagebox.askyesno(APP_NAME, msg, icon="warning" if perm else "question", default="no"):
                return
            try:
                self.busy("Siliniyor...")
                out = delete_mods(self.res.game_root, chosen, perm)
            except SafetyError as e:
                self.idle()
                messagebox.showerror(APP_NAME, "İşlem durduruldu, hiçbir şey silinmedi:\n\n%s" % e)
                return
            self.idle()
            ok = sum(1 for r in out if r["status"] == "silindi")
            bad = [r for r in out if r["status"] != "silindi"]
            text = "%d mod işlendi: %d başarılı" % (len(out), ok)
            if bad:
                text += ", %d sorunlu:\n\n" % len(bad) + "\n".join(
                    "• %s — %s %s" % (r["name"], r["status"], r.get("reason", "")) for r in bad)
            messagebox.showinfo(APP_NAME, text + "\n\nİşlem geçmişe kaydedildi.")
            self.checked = set()
            self.do_scan()

        # ------------------------------------------------------------------ TAM KARŞILAŞTIRMA sekmesi
        def bl_refresh_status(self):
            p = self.path_var.get().strip().strip('"')
            b = load_baseline(p) if is_game_dir(p) else None
            self.bl = b
            if b:
                self.bl_status.config(text="TEMEL KAYITLI  •  %s  •  %d dosya"
                                      % (b.get("created", "?"), b.get("file_count", 0)),
                                      foreground=T["green"])
            else:
                self.bl_status.config(text="TEMEL YOK  —  önce 'Temel Oluştur' ile kaydet",
                                      foreground=T["orange"])

        def bl_create(self):
            root = self.cur_root()
            if not root:
                return
            if self.bl and not messagebox.askyesno(
                    APP_NAME, "Bu oyun için zaten bir temel var (%s). Üzerine yazılsın mı?\n\n"
                    "Yalnızca hiç mod kurulu değilken veya oyun güncellendiğinde yeniden oluştur."
                    % self.bl.get("created", "?"), icon="warning", default="no"):
                return
            try:
                self.busy("Temel oluşturuluyor, oyun klasörü taranıyor...")
                data = build_baseline(root, progress_cb=lambda n: self.status.config(
                    text="Temel oluşturuluyor... %d dosya" % n) or self.update())
                save_baseline(root, data)
            except SafetyError as e:
                self.idle()
                messagebox.showerror(APP_NAME, str(e))
                return
            self.bl = data
            self.bl_diff = None
            self.bl_checked = set()
            self.bl_tree.delete(*self.bl_tree.get_children())
            self.bl_override_chk.pack_forget()
            self.bl_warn.config(text="")
            self.bl_info.config(text="")
            self.bl_refresh_status()
            self.idle()
            messagebox.showinfo(APP_NAME, "Temel oluşturuldu: %d dosya kaydedildi." % data["file_count"])

        def bl_compare(self, quiet=False):
            root = self.cur_root()
            if not root:
                return
            if not self.bl:
                messagebox.showinfo(APP_NAME, "Önce 'Temel Oluştur' ile bir temel kaydet.")
                return
            try:
                self.busy("Karşılaştırılıyor...")
                d = diff_baseline(root, self.bl)
            except SafetyError as e:
                self.idle()
                messagebox.showerror(APP_NAME, str(e))
                return
            self.idle()
            self.bl_diff = d
            self.bl_checked = set()
            self.bl_tree.delete(*self.bl_tree.get_children())
            for it in d["items"]:
                self.bl_tree.insert("", "end", iid=it.key, values=(
                    CHK_OFF, it.name + ("   [içinde program var — silinmez]" if it.has_self else ""),
                    it.files, fmt_size(it.size)))
            if d["version_mismatch"]:
                self.bl_warn.config(text="UYARI: Oyunun ana .exe dosyası temelden beri değişmiş (oyun güncellenmiş "
                                         "olabilir). Yeni/değişen resmi dosyalar yanlışlıkla 'sonradan eklenmiş' "
                                         "görünebilir. Temeli yenilemen önerilir.")
                self.bl_override_chk.pack(side="left", padx=18)
            else:
                self.bl_warn.config(text="")
                self.bl_override_chk.pack_forget()
            info = "Yeni öğe: %d" % len(d["items"])
            if d["changed_files"]:
                info += "    •    %d özgün dosya değişmiş görünüyor (silinmez, bilgi amaçlı)" % len(d["changed_files"])
            if d["walk_errors"]:
                info += "    •    %d yol okunamadı (liste eksik olabilir)" % len(d["walk_errors"])
            self.bl_info.config(text=info)
            if not d["items"] and not quiet:
                messagebox.showinfo(APP_NAME, "Temelden bu yana yeni/harici bir şey bulunamadı.")

        def bl_toggle(self, iid):
            if iid in self.bl_checked:
                self.bl_checked.discard(iid)
            else:
                self.bl_checked.add(iid)
            self.bl_tree.set(iid, "sel", CHK_ON if iid in self.bl_checked else CHK_OFF)

        def bl_on_click(self, ev):
            if self.bl_tree.identify_region(ev.x, ev.y) == "cell":
                iid = self.bl_tree.identify_row(ev.y)
                if iid:
                    self.bl_tree.selection_set(iid)
                    self.bl_toggle(iid)
                    return "break"

        def bl_select_all(self):
            if self.bl_diff:
                for it in self.bl_diff["items"]:
                    self.bl_checked.add(it.key)
                    self.bl_tree.set(it.key, "sel", CHK_ON)

        def bl_select_none(self):
            for iid in list(self.bl_checked):
                self.bl_tree.set(iid, "sel", CHK_OFF)
            self.bl_checked = set()

        def bl_delete(self):
            root = self.cur_root()
            d = self.bl_diff
            if not root:
                return
            if not d or not self.bl_checked:
                messagebox.showinfo(APP_NAME, "Önce karşılaştır ve silinecekleri işaretle.")
                return
            if d["version_mismatch"] and not self.bl_override.get():
                messagebox.showwarning(APP_NAME, "Sürüm uyuşmazlığı var. Devam etmek için onay kutusunu işaretle.")
                return
            chosen = [it for it in d["items"] if it.key in self.bl_checked]
            names = "\n".join("• " + c.name for c in chosen[:15])
            perm = self.bl_perm.get()
            if not messagebox.askyesno(APP_NAME, "%d öğe %s:\n\n%s\n\nDevam edilsin mi?"
                                       % (len(chosen), "KALICI SİLİNECEK" if perm else "yedeğe taşınacak", names),
                                       icon="warning" if perm else "question", default="no"):
                return
            try:
                self.busy("Siliniyor...")
                out = delete_baseline_items(root, self.bl, chosen, perm)
            except SafetyError as e:
                self.idle()
                messagebox.showerror(APP_NAME, "İşlem durduruldu:\n\n%s" % e)
                return
            self.idle()
            ok = sum(1 for r in out if r["status"] == "silindi")
            skipped = [r for r in out if r["status"] != "silindi"]
            text = "%d/%d öğe silindi. Geçmişe kaydedildi." % (ok, len(out))
            if skipped:
                text += "\n\nSilinmeyenler:\n" + "\n".join(
                    "• %s — %s" % (r["name"], r.get("reason") or r["status"]) for r in skipped[:10])
            messagebox.showinfo(APP_NAME, text)
            self.bl_compare(quiet=True)
            self.do_scan()

        def bl_reset(self):
            root = self.cur_root()
            if not root:
                return
            if not self.bl:
                messagebox.showinfo(APP_NAME, "Önce 'Temel Oluştur' ile bir temel kaydet.")
                return
            try:
                self.busy("Karşılaştırılıyor...")
                d = diff_baseline(root, self.bl)
            except SafetyError as e:
                self.idle()
                messagebox.showerror(APP_NAME, str(e))
                return
            self.idle()
            if d["version_mismatch"]:
                messagebox.showerror(APP_NAME, "Oyunun .exe dosyası temelden beri değişmiş (oyun güncellenmiş "
                                     "olabilir). Tam sıfırlama güvenlik için engellendi.\n\nÖnce oyunu "
                                     "Steam/GOG/Epic'ten 'dosya bütünlüğünü doğrula' ile onar, sonra "
                                     "'Temel Oluştur / Güncelle' ile temeli yenile.")
                return
            if not d["items"]:
                messagebox.showinfo(APP_NAME, "Temelden bu yana eklenmiş hiçbir şey yok. Oyun zaten ilk halinde.")
                return
            perm = self.bl_perm.get()
            total = sum(i.size for i in d["items"])
            files = sum(i.files for i in d["items"])
            names = "\n".join("• " + i.name for i in d["items"][:20])
            if len(d["items"]) > 20:
                names += "\n… ve %d öğe daha" % (len(d["items"]) - 20)
            msg = ("TAM SIFIRLAMA: temelde olmayan %d öğe (%d dosya, %s) %s.\n\n%s\n\n"
                   "Bu, korumalı klasörlerdeki (archive/pc/patch, engine vb.) sonradan eklenmiş dosyaları da "
                   "kapsar. Devam edilsin mi?"
                   % (len(d["items"]), files, fmt_size(total),
                      "KALICI OLARAK SİLİNECEK (geri dönüş yok)" if perm else "yedek klasörüne taşınacak", names))
            if not messagebox.askyesno(APP_NAME, msg, icon="warning", default="no"):
                return
            typed = simpledialog.askstring(APP_NAME, "Onaylamak için SIFIRLA yaz:", parent=self)
            if (typed or "").strip() != "SIFIRLA":
                messagebox.showinfo(APP_NAME, "İptal edildi. Hiçbir şey silinmedi.")
                return
            try:
                self.busy("Sıfırlanıyor ve doğrulanıyor...")
                r = reset_to_pristine(root, self.bl, perm)
            except SafetyError as e:
                self.idle()
                messagebox.showerror(APP_NAME, "İşlem durduruldu:\n\n%s" % e)
                return
            self.idle()
            lines = []
            if r["clean"]:
                lines.append("DOĞRULANDI: Temelden sonra eklenen hiçbir dosya/klasör kalmadı.")
            else:
                if r["leftovers"]:
                    lines.append("TEMİZ DEĞİL: %d öğe silinemedi (kilitli/izin sorunu olabilir):" % len(r["leftovers"]))
                    lines += ["• " + k for k in r["leftovers"][:20]]
                if r["unreadable"]:
                    lines.append("DOĞRULANAMADI: %d yol okunamadı (ör. %s)." % (len(r["unreadable"]), r["unreadable"][0]))
                lines.append("\nOyunu, Vortex/başlatıcıları ve antivirüsü kapatıp tekrar dene; gerekirse programı "
                             "yönetici olarak çalıştır.")
            lines.append("\nİşlenen öğe: %d" % r["removed"])
            if r["changed_files"] or r["missing_files"]:
                lines.append("\nDİKKAT: Temeldeki özgün dosyalardan %d tanesi DEĞİŞMİŞ, %d tanesi EKSİK. Bir mod "
                             "orijinal dosyanın üzerine yazmış olabilir; bunları bu program geri getiremez. Tam "
                             "özgün hâl için Steam/GOG/Epic'ten 'dosya bütünlüğünü doğrula' yap."
                             % (len(r["changed_files"]), len(r["missing_files"])))
            else:
                lines.append("Özgün dosyaların hiçbiri değişmemiş veya eksik değil.")
            lines.append("\nNot: r6/cache ve ekran görüntüsü klasörleri her zaman dışarıda tutulur.")
            messagebox.showinfo(APP_NAME, "\n".join(lines))
            self.bl_compare(quiet=True)
            self.do_scan()

        # ------------------------------------------------------------------ GEÇMİŞ sekmesi
        def refresh_history(self):
            self.hist = load_history()
            self.hist_index = {}
            tv = self.hist_tree
            tv.delete(*tv.get_children())
            for bi in range(len(self.hist) - 1, -1, -1):
                b = self.hist[bi]
                for ii, it in enumerate(b["items"]):
                    iid = "%d:%d" % (bi, ii)
                    self.hist_index[iid] = (bi, ii)
                    tv.insert("", "end", iid=iid, values=(
                        b["time"], it["name"], it["status"],
                        "Yedeğe taşındı" if b["mode"].startswith("yedek") else "Kalıcı silindi"))

        def restore_selected(self):
            sel = self.hist_tree.selection()
            if not sel:
                messagebox.showinfo(APP_NAME, "Önce listeden geri yüklenecek öğeyi seç.")
                return
            bi, ii = self.hist_index[sel[0]]
            msgs = restore_item(self.hist[bi], self.hist[bi]["items"][ii])
            try:
                save_history(self.hist)
            except OSError:
                pass
            messagebox.showinfo(APP_NAME, "\n".join(msgs) or "Yapılacak bir şey yok.")
            self.refresh_history()
            self.do_scan()

    App().mainloop()


# --------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--cli", action="store_true", help="metin modunda çalıştır (python ile, .pyw ile değil)")
    ap.add_argument("--yol", help="oyun klasörünün yolu")
    args = ap.parse_args()

    game = args.yol
    if not args.cli:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            print("tkinter bulunamadı, metin moduna geçiliyor (Ubuntu: sudo apt install python3-tk).")
        else:
            try:
                gui_main(game)
            except Exception:
                import traceback
                tb = traceback.format_exc()
                log_error(tb)
                try:
                    from tkinter import messagebox
                    messagebox.showerror(APP_NAME, "Program başlatılamadı:\n\n%s\n\nAyrıntı: %s"
                                         % (tb.strip().splitlines()[-1], DATA_DIR / "hata.log"))
                except Exception:
                    pass
            return
    if not game:
        found = find_game_dirs()
        if found:
            game = str(found[0])
            print("Oyun klasörü bulundu: %s" % game)
        else:
            game = input("Oyun klasörünün yolunu yaz: ").strip().strip('"')
    try:
        cli_main(game)
    except SafetyError as e:
        print("HATA: %s" % e)
        sys.exit(1)


if __name__ == "__main__":
    main()
