import json, os, zipfile, tempfile, unittest, shutil, stat
from unittest import mock
from pathlib import Path
import cs2cfg_core as c

NOT_RUNNING = lambda *a: False
KEYS = '"config"\n{\n\t"bindings"\n\t{\n\t\t"W"\t\t"+forward"\n\t\t"`"\t\t"<unbound>"\n\t\t"MOUSE1"\t\t"+attack"\n\t}\n}\n'
CONV = '"config"\n{\n\t"convars"\n\t{\n\t\t"cl_crosshairsize"\t\t"2"\n\t\t"sensitivity"\t\t"1.2"\n\t}\n}\n'
VID_OLD = '"video.cfg"\n{\n\t"setting.fullscreen"\t\t"1"\n\t"VendorID"\t\t"0x10de"\n\t"DeviceID"\t\t"0x1111"\n}\n'
VID_NEW = '"video.cfg"\n{\n\t"setting.fullscreen"\t\t"0"\n\t"VendorID"\t\t"0x1002"\n\t"DeviceID"\t\t"0x2222"\n}\n'
W = lambda p, t: Path(p).write_text(t, encoding="utf-8")

def make_steam(tmp):
    steam = Path(tmp) / "Steam"
    cfg = steam / "userdata" / "123456" / "730" / "local" / "cfg"; cfg.mkdir(parents=True)
    W(cfg / "cs2_user_keys_0_slot0.vcfg", KEYS); W(cfg / "cs2_user_convars_0_slot0.vcfg", CONV)
    W(cfg / "cs2_video.txt", VID_OLD)
    game = steam / "steamapps/common/Counter-Strike Global Offensive/game/csgo/cfg"; game.mkdir(parents=True)
    W(game / "autoexec.cfg", 'bind "f" "+lookatweapon"\n'); W(game / "valve_default.cfg", "// valve\n")
    W(steam / "steamapps/libraryfolders.vdf", '"libraryfolders"\n{\n"0"\n{\n"path"\t"%s"\n}\n}\n' % steam)
    (steam / "config").mkdir()
    W(steam / "config/loginusers.vdf", '"users"\n{\n"%d"\n{\n"AccountName"\t"acc"\n"PersonaName"\t"Игрок"\n}\n}\n' % (76561197960265728 + 123456))
    return steam

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.steam = make_steam(self.tmp.name)
        self.acc = c.find_accounts(self.steam)[0]; self.game = c.find_game_cfg(self.steam)
        self.store = c.ProfileStore(Path(self.tmp.name) / "store")
        self.keys = self.acc.cfg_dir / "cs2_user_keys_0_slot0.vcfg"
    def tearDown(self): self.tmp.cleanup()
    def restore(self, p, **kw): return self.store.restore(p, self.acc, self.game, running_check=NOT_RUNNING, **kw)

class Discovery(Base):
    def test_discovery(self):
        self.assertEqual(self.acc.name, "Игрок"); self.assertTrue(self.game.as_posix().endswith("csgo/cfg"))
        self.assertEqual(c.find_steam_root(str(self.steam)), self.steam)
    def test_dirs_instead_of_files_do_not_crash(self):
        shutil.rmtree(self.steam / "config/loginusers.vdf", ignore_errors=True)
        (self.steam / "config/loginusers.vdf").unlink(); (self.steam / "config/loginusers.vdf").mkdir()
        (self.steam / "steamapps/libraryfolders.vdf").unlink(); (self.steam / "steamapps/libraryfolders.vdf").mkdir()
        self.assertEqual(c.find_accounts(self.steam)[0].name, ""); self.assertIsNotNone(c.find_game_cfg(self.steam))

class Parse(Base):
    def test_parse(self):
        b = c.read_binds(self.keys); self.assertEqual(b["W"], "+forward"); self.assertEqual(b["`"], "<unbound>")
        self.assertEqual(c.read_convars(self.acc.cfg_dir / "cs2_user_convars_0_slot0.vcfg")["sensitivity"], "1.2")
        self.assertEqual(c.read_binds(self.acc.cfg_dir / "missing.vcfg"), {})
    def test_unbalanced_braces_rejected(self):
        for bad in ('"config" { "bindings" { "W" "x" }', '"config" { "bindings" { "W" "x" } } }', '"a" "b" }', '"k"'):
            with self.assertRaises(ValueError, msg=bad): c.parse_kv(bad)
    def test_unreadable_is_configerror(self):
        self.keys.unlink(); self.keys.mkdir()  # IsADirectoryError / PermissionError -> OSError
        with self.assertRaises(c.ConfigError): c.read_binds(self.keys)
    def test_corrupt(self):
        W(self.keys, '"config" { "bindings" { "W" ')
        with self.assertRaises(c.ConfigError): c.read_binds(self.keys)
    def test_autoexec_quotes(self):
        t = c.to_autoexec({"W": "+forward", "`": "<unbound>", "X": 'say \\"hi\\"', "Y": 'say "yo"'}, {"sensitivity": "1.2"}, "echo hi")
        self.assertIn('bind "W" "+forward"', t); self.assertIn('unbind "`"', t)
        self.assertIn("\"say 'hi'\"", t); self.assertIn("\"say 'yo'\"", t); self.assertNotIn("\\", t)
        self.assertTrue(t.rstrip().endswith("host_writeconfig"))
    def test_diff(self):
        self.assertEqual(c.diff_sections({"a": "1", "b": "2"}, {"a": "1", "b": "3", "c": "4"}), ["~ b: 2 -> 3", "+ c = 4"])

class Profiles(Base):
    def test_roundtrip_and_lastclouded(self):
        p = self.store.save("Мой", self.acc, self.game)
        self.assertIn("game/autoexec.cfg", p.files); self.assertNotIn("game/valve_default.cfg", p.files)  # только autoexec
        W(self.keys, "garbage"); (self.game / "autoexec.cfg").unlink()
        done, warns = self.restore(p)
        self.assertEqual(self.keys.read_text(encoding="utf-8"), KEYS)
        self.assertEqual((self.acc.cfg_dir / "cs2_user_keys_0_slot0.vcfg_lastclouded").read_text(encoding="utf-8"), KEYS)
        self.assertTrue((self.game / "autoexec.cfg").exists()); self.assertEqual(warns, [])
        a = [x for x in self.store.list() if x.is_auto]; self.assertEqual(len(a), 1)
        self.assertEqual((a[0].path / "user/cs2_user_keys_0_slot0.vcfg").read_text(encoding="utf-8"), "garbage")
    def test_all_cfg_is_opt_in(self):
        p = self.store.save("All", self.acc, self.game, game_names=tuple(c.list_game_cfgs(self.game)))
        self.assertIn("game/valve_default.cfg", p.files)
    def test_no_partial_restore_when_game_missing(self):  # баг: бинды применялись до проверки папки игры
        p = self.store.save("P", self.acc, self.game); W(self.keys, "changed")
        with self.assertRaises(c.ConfigError): self.restore_nogame(p)
        self.assertEqual(self.keys.read_text(encoding="utf-8"), "changed")
        self.assertEqual([x for x in self.store.list() if x.is_auto], [])
    def restore_nogame(self, p): return self.store.restore(p, self.acc, None, running_check=NOT_RUNNING)
    def test_video_not_applied_without_local_file(self):  # баг: чужие VendorID попадали на новый ПК
        p = self.store.save("V", self.acc, self.game); (self.acc.cfg_dir / "cs2_video.txt").unlink()
        done, warns = self.restore(p)
        self.assertFalse((self.acc.cfg_dir / "cs2_video.txt").exists()); self.assertTrue(warns); self.assertNotIn("cs2_video.txt", done)
    def test_video_keeps_gpu_keys(self):
        m = c.merge_video(VID_NEW, VID_OLD)
        self.assertIn('"VendorID"\t\t"0x10de"', m); self.assertIn('"setting.fullscreen"\t\t"0"', m)
    def test_game_running_blocks(self):
        p = self.store.save("R", self.acc, self.game)
        with self.assertRaises(c.ConfigError): self.store.restore(p, self.acc, self.game, running_check=lambda *a: True)
    def test_internal_name_bypass_closed(self):  # баг: "auto-backup a/../../escaped"
        with self.assertRaises(c.ConfigError): self.store.save("auto-backup a/../../escaped", self.acc, self.game)
        self.assertFalse((Path(self.tmp.name) / "escaped").exists())
    def test_names(self):
        for bad in ("", "  ", "a/b", "CON", "con.cfg", "nul.txt", 'x"y', "a" * 81):
            with self.assertRaises(c.ConfigError, msg=bad): c.validate_name(bad)
        self.store.save("A", self.acc, self.game)
        with self.assertRaises(c.ConfigError): self.store.save("A", self.acc, self.game)
        self.store.save("A", self.acc, self.game, overwrite=True)
    def test_empty_account(self):
        e = c.Account("9", Path(self.tmp.name) / "e"); e.cfg_dir.mkdir()
        with self.assertRaises(c.NoFilesError): self.store.save("X", e, None)
    def test_backups_unique_and_rotated(self):  # баг: два восстановления за секунду давали один бэкап
        p = self.store.save("B", self.acc, self.game)
        for _ in range(15): self.restore(p)
        autos = [x for x in self.store.list() if x.is_auto]; self.assertEqual(len(autos), c.KEEP_AUTO)
    def test_unexpected_backup_error_aborts(self):  # баг: except ConfigError: pass глотал всё
        p = self.store.save("E", self.acc, self.game); W(self.keys, "changed")
        def boom(*a, **k): raise c.ConfigError("диск полон")
        self.store.save = boom
        with self.assertRaises(c.ConfigError): self.restore(p)
        self.assertEqual(self.keys.read_text(encoding="utf-8"), "changed")
    def test_rename_delete(self):
        self.store.save("Old", self.acc, self.game); self.store.rename("Old", "New")
        self.assertEqual([p.name for p in self.store.list()], ["New"]); self.store.delete("New")
    def test_atomic_write_backup(self):
        t = self.game / "autoexec.cfg"; old = t.read_text(encoding="utf-8")
        bak = c.write_text_atomic(t, "new", backup=True)
        self.assertEqual(bak.read_text(encoding="utf-8"), old); self.assertEqual(t.read_text(encoding="utf-8"), "new")
        self.assertEqual([p.name for p in self.game.iterdir() if p.name.startswith(".tmp_")], [])

class Zip(Base):
    def test_roundtrip_marks_imported(self):
        self.store.save("Z", self.acc, self.game); z = Path(self.tmp.name) / "z.zip"; self.store.export_zip("Z", z)
        imp = self.store.import_zip(z); self.assertEqual(imp.name, "Z (2)"); self.assertTrue(imp.imported)
    def _zip(self, files):
        p = Path(self.tmp.name) / "t.zip"
        with zipfile.ZipFile(p, "w") as f:
            for n, d in files.items(): f.writestr(n, d)
        return p
    def test_rejects(self):
        cases = {"slip": {"meta.json": "{}", "../evil.txt": "x"}, "nested": {"meta.json": "{}", "game/a/b.cfg": "x"},
                 "badext": {"meta.json": "{}", "game/evil.exe": "x"}, "baduser": {"meta.json": "{}", "user/x.vcfg": "x"},
                 "nometa": {"user/cs2_video.txt": "x"}, "bigfile": {"meta.json": "{}", "game/a.cfg": "x" * 1_000_001},
                 "many": {"meta.json": "{}", **{f"game/{i}.cfg": "x" for i in range(201)}}}
        for k, files in cases.items():
            with self.assertRaises(c.ConfigError, msg=k): self.store.import_zip(self._zip(files))
        bad = Path(self.tmp.name) / "bad.zip"; W(bad, "nope")
        with self.assertRaises(c.ConfigError): self.store.import_zip(bad)


class Poison(Base):
    def _profile_dir(self, meta):
        d = self.store.root / "bad"; d.mkdir(); W(d / "meta.json", meta); return d
    def test_poison_meta_does_not_break_listing(self):
        self.store.save("Good", self.acc, self.game)
        for meta in ('{"created": null}', '{"created": 5}', "[]", '"x"', '{"files": 5}', '{"files": [1]}', "{broken"):
            d = self._profile_dir(meta)
            self.assertEqual([p.name for p in self.store.list()], ["Good"], meta); shutil.rmtree(d)
    def test_import_rejects_bad_meta_before_creating_folder(self):
        for meta in ('[]', '"x"', '{"created": 5}', '{"files": 5}', '{"note": 1}'):
            p = Path(self.tmp.name) / "m.zip"
            with zipfile.ZipFile(p, "w") as f: f.writestr("meta.json", meta); f.writestr("user/cs2_video.txt", VID_OLD)
            with self.assertRaises(c.ConfigError, msg=meta): self.store.import_zip(p)
            self.assertEqual([d.name for d in self.store.root.iterdir()], [], meta)
    def test_deep_nesting_is_error_not_crash(self):
        W(self.keys, '"a" {' * 5000 + "}" * 5000)
        with self.assertRaises(c.ConfigError): c.read_binds(self.keys)

class Review3(Base):
    def test_user_profile_named_like_backup_is_blocked_and_not_rotated(self):  # п.1
        with self.assertRaises(c.ConfigError): self.store.save("auto-backup моя лучшая", self.acc, self.game)
        with self.assertRaises(c.ConfigError): self.store.rename(self.store.save("X", self.acc, self.game).name, "Auto-Backup y")
        # даже если такая папка уже есть от старой версии: без флага auto она не тронется ротацией
        d = self.store.root / "auto-backup old"; shutil.copytree(self.store.save("T", self.acc, self.game).path, d)
        m = json.loads((d / "meta.json").read_text(encoding="utf-8")); m["name"] = "auto-backup old"
        W(d / "meta.json", json.dumps(m)); p = self.store.save("R", self.acc, self.game)
        for _ in range(13): self.restore(p)
        self.assertIn("auto-backup old", [x.name for x in self.store.list()])
    def test_bak_keeps_original(self):  # п.2
        t = self.game / "autoexec.cfg"; W(t, "original")
        b1 = c.write_text_atomic(t, "gen1", backup=True); b2 = c.write_text_atomic(t, "gen2", backup=True)
        self.assertEqual(b1.read_text(encoding="utf-8"), "original"); self.assertEqual(b2.read_text(encoding="utf-8"), "gen1")
        self.assertNotEqual(b1, b2)
    def test_only_video_without_local_file_returns_warning(self):  # п.3
        p = self.store.save("V", self.acc, self.game); (self.acc.cfg_dir / "cs2_video.txt").unlink()
        done, warns = self.restore(p, parts=("video",)); self.assertEqual(done, []); self.assertTrue(warns)
    def test_meta_not_dict_in_zip(self):  # п.4
        for meta in ("[]", '"x"'):
            p = Path(self.tmp.name) / "z.zip"
            with zipfile.ZipFile(p, "w") as f: f.writestr("meta.json", meta)
            with self.assertRaises(c.ConfigError): self.store.import_zip(p)
    def test_overwrite_is_atomic(self):
        self.store.save("O", self.acc, self.game, note="old"); real = os.replace; n = {"i": 0}
        def flaky(a, b):
            n["i"] += 1
            if "building_" in str(a) and str(b).endswith("O"): raise PermissionError("antivirus")
            return real(a, b)
        with mock.patch("os.replace", flaky), self.assertRaises(PermissionError):
            self.store.save("O", self.acc, self.game, note="new", overwrite=True)
        self.assertEqual(self.store.get("O").note, "old")
    def test_export_anonymizes_by_default(self):
        self.store.save("E", self.acc, self.game, note="секрет"); z = Path(self.tmp.name) / "e.zip"
        self.store.export_zip("E", z)
        m = json.loads(zipfile.ZipFile(z).read("meta.json")); self.assertEqual((m["account_id"], m["note"]), ("", ""))
        self.store.export_zip("E", z, anonymize=False)
        self.assertEqual(json.loads(zipfile.ZipFile(z).read("meta.json"))["account_id"], "123456")
    def test_import_gets_fresh_date_and_not_auto(self):
        self.store.save("Q", self.acc, self.game); z = Path(self.tmp.name) / "q.zip"; self.store.export_zip("Q", z)
        m = {"name": "auto-backup q", "files": [], "created": "2000-01-01 00:00:00", "auto": True}
        with zipfile.ZipFile(z, "a") as f: pass
        p = Path(self.tmp.name) / "a.zip"
        with zipfile.ZipFile(p, "w") as f: f.writestr("meta.json", json.dumps(m)); f.writestr("user/cs2_video.txt", VID_OLD)
        imp = self.store.import_zip(p); self.assertFalse(imp.is_auto); self.assertGreater(imp.created, "2020")
        self.assertFalse(imp.name.lower().startswith("auto-backup"))
    def test_empty_autoexec(self):
        self.assertEqual(c.to_autoexec({}, {}), ""); self.assertTrue(c.autoexec_is_empty("// x\nhost_writeconfig\n"))
        self.assertFalse(c.autoexec_is_empty('bind "a" "b"\nhost_writeconfig'))
        self.assertEqual(c.to_autoexec({}, {}, "echo hi").count("echo hi"), 1)
    def test_rollback_when_game_write_fails(self):
        p = self.store.save("RB", self.acc, self.game); W(self.keys, "changed"); real = c._atomic_copy
        def fail_game(src, dst):
            if Path(dst).parent == self.game: raise PermissionError("Program Files")
            return real(src, dst)
        with mock.patch.object(c, "_atomic_copy", fail_game), self.assertRaises(c.ConfigError):
            self.restore(p)
        self.assertEqual(self.keys.read_text(encoding="utf-8"), "changed")
    def test_readonly_target_is_replaced(self):
        t = self.game / "autoexec.cfg"; os.chmod(t, stat.S_IREAD)
        c._clear_readonly(t); c.write_text_atomic(t, "new"); self.assertEqual(t.read_text(encoding="utf-8"), "new")
    def test_running_check_unknown_is_none(self):
        with mock.patch("subprocess.run", side_effect=OSError("x")): self.assertIsNone(c.is_running("cs2.exe"))
    def test_compare_dirs_reports_missing_files(self):
        p = self.store.save("C", self.acc, self.game); (p.path / "user" / "cs2_video.txt").unlink()
        W(self.keys, KEYS.replace("+forward", "+back"))
        out = "\n".join(c.compare_dirs(self.acc.cfg_dir, p.path / "user"))
        self.assertIn("~ W: +back -> +forward", out); self.assertIn("в профиле нет файла cs2_video.txt", out)


class Review4(Base):
    def _fail_on(self, parent):
        real = c._atomic_copy
        def f(src, dst):
            if Path(dst).parent == parent: raise PermissionError("Program Files")
            return real(src, dst)
        return mock.patch.object(c, "_atomic_copy", f)
    def test_rollback_deletes_files_created_on_fresh_pc(self):
        p = self.store.save("F", self.acc, self.game)
        for f in list(self.acc.cfg_dir.iterdir()): f.unlink()   # «свежий ПК»: конфига нет
        (self.game / "autoexec.cfg").unlink()
        with self._fail_on(self.game), self.assertRaises(c.ConfigError):
            self.restore(p)
        self.assertEqual(sorted(x.name for x in self.acc.cfg_dir.iterdir()), [])
        self.assertFalse((self.game / "autoexec.cfg").exists())
    def test_rollback_deletes_new_autoexec_but_restores_old_user_files(self):
        p = self.store.save("G", self.acc, self.game); W(self.keys, "changed")
        (self.game / "autoexec.cfg").unlink()
        real = c._atomic_copy; calls = {"n": 0}
        def late_fail(src, dst):
            real(src, dst)
            if Path(dst).name == "autoexec.cfg": raise OSError("disk full after write")
        with mock.patch.object(c, "_atomic_copy", late_fail), self.assertRaises(c.ConfigError) as cm:
            self.restore(p)
        self.assertFalse((self.game / "autoexec.cfg").exists()); self.assertEqual(self.keys.read_text(encoding="utf-8"), "changed")
        self.assertIn("удалены", str(cm.exception))
    def test_unknown_process_state_requires_confirmation(self):
        p = self.store.save("U", self.acc, self.game); W(self.keys, "changed")
        with self.assertRaises(c.UnknownStateError): self.store.restore(p, self.acc, self.game, running_check=lambda *a: None)
        self.assertEqual(self.keys.read_text(encoding="utf-8"), "changed")
        self.store.restore(p, self.acc, self.game, running_check=lambda *a: None, confirm_unknown=True)
        self.assertEqual(self.keys.read_text(encoding="utf-8"), KEYS)
    def test_old_auto_backups_are_migrated(self):
        p = self.store.save("src", self.acc, self.game)
        for i in range(12):
            name = f"auto-backup 2026-01-{i + 1:02d} 10-00-00"; d = self.store.root / name; shutil.copytree(p.path, d)
            W(d / "meta.json", json.dumps({"name": name, "note": c.AUTO_NOTE, "created": f"2026-01-{i + 1:02d} 10:00:00", "files": p.files}))
        self.assertTrue(all(x.is_auto for x in self.store.list() if x.name.startswith("auto-backup")))
        self.restore(p); self.assertEqual(len([x for x in self.store.list() if x.is_auto]), c.KEEP_AUTO)
    def test_skipped_profiles_counted(self):
        self.store.save("ok", self.acc, self.game); d = self.store.root / "bad"; d.mkdir(); W(d / "meta.json", "[]")
        self.store.list(); self.assertEqual(self.store.skipped, 1)
    def test_bak_count_limited(self):
        t = self.game / "autoexec.cfg"
        for i in range(12): c.write_text_atomic(t, f"v{i}", backup=True); os.utime(t, (i + 1, i + 1))
        baks = [p.name for p in self.game.glob("autoexec.cfg*.bak")]
        self.assertLessEqual(len(baks), c.KEEP_BAK + 1); self.assertIn("autoexec.cfg.bak", baks)
    def test_readonly_attribute_is_preserved(self):
        t = self.game / "autoexec.cfg"; os.chmod(t, stat.S_IREAD)
        c.write_text_atomic(t, "new"); self.assertEqual(t.read_text(encoding="utf-8"), "new")
        self.assertFalse(t.stat().st_mode & stat.S_IWRITE)
        src = self.game / "src.txt"; W(src, "copy"); c._atomic_copy(src, t)
        self.assertEqual(t.read_text(encoding="utf-8"), "copy"); self.assertFalse(t.stat().st_mode & stat.S_IWRITE)

class Review5(Base):
    def test_backslash_key_is_not_escape(self):  # баг: "\" (клавиша \) ломал разбор всех биндов аккаунта
        W(self.keys, '"config"\n{\n\t"bindings"\n\t{\n\t\t"8"\t\t"toggle cl_showfps 0 1”"\n\t\t"\\"\t\t"toggleconsole"\n'
                     '\t\t"DEL"\t\t"voice_modenable_toggle"\n\t}\n}\n')
        b = c.read_binds(self.keys); self.assertEqual(b["\\"], "toggleconsole"); self.assertEqual(len(b), 3)
        self.assertIn('bind "\\" "toggleconsole"', c.to_autoexec(b, {}))
    def test_escaped_quotes_still_parse(self):
        self.assertEqual(c.parse_kv('"a" { "k" "say \\"hi\\"" }')["a"]["k"], 'say \\"hi\\"')
    def test_binds_only_profile_and_restore(self):
        p = self.store.save("Бинды", self.acc, self.game, game_names=(), parts=("binds",))
        self.assertEqual(p.files, ["user/cs2_user_keys_0_slot0.vcfg"]); self.assertEqual(p.parts, ["binds"])
        conv = self.acc.cfg_dir / "cs2_user_convars_0_slot0.vcfg"; W(conv, "мои настройки"); W(self.keys, "чужие")
        done, _ = self.restore(p); self.assertEqual(done, ["cs2_user_keys_0_slot0.vcfg"])
        self.assertEqual(self.keys.read_text(encoding="utf-8"), KEYS); self.assertEqual(conv.read_text(encoding="utf-8"), "мои настройки")
    def test_restore_only_binds_from_full_profile(self):
        p = self.store.save("Full", self.acc, self.game)
        conv = self.acc.cfg_dir / "cs2_user_convars_0_slot0.vcfg"; W(conv, "keep"); W(self.keys, "x")
        self.restore(p, parts=("binds",))
        self.assertEqual(self.keys.read_text(encoding="utf-8"), KEYS); self.assertEqual(conv.read_text(encoding="utf-8"), "keep")
    def test_rollback_restores_exact_lastclouded(self):
        p = self.store.save("L", self.acc, self.game); last = self.acc.cfg_dir / "cs2_user_keys_0_slot0.vcfg_lastclouded"
        W(last, "облако"); W(self.keys, "локально")
        with Review4._fail_on(self, self.game), self.assertRaises(c.ConfigError):
            self.restore(p)
        self.assertEqual(last.read_text(encoding="utf-8"), "облако"); self.assertEqual(self.keys.read_text(encoding="utf-8"), "локально")
    def test_transfer_binds_between_accounts(self):
        other = c.Account("777", Path(self.tmp.name) / "o"); other.cfg_dir.mkdir()
        conv = other.cfg_dir / "cs2_user_convars_0_slot0.vcfg"; W(conv, "свои"); W(other.cfg_dir / "cs2_user_keys_0_slot0.vcfg", "старые")
        done, _ = self.store.transfer(self.acc, other, ("binds",), running_check=NOT_RUNNING)
        self.assertEqual(done, ["cs2_user_keys_0_slot0.vcfg"]); self.assertEqual(conv.read_text(encoding="utf-8"), "свои")
        self.assertEqual((other.cfg_dir / "cs2_user_keys_0_slot0.vcfg").read_text(encoding="utf-8"), KEYS)
        self.assertFalse(any(d.name.startswith(".transfer") for d in self.store.root.iterdir()))
        with self.assertRaises(c.ConfigError): self.store.transfer(self.acc, self.acc, running_check=NOT_RUNNING)
    def test_accounts_without_cs2_are_listed(self):  # баг: аккаунт без запуска CS2 не показывался
        (self.steam / "userdata" / "555").mkdir(); (self.steam / "userdata" / "0").mkdir()
        W(self.steam / "config/loginusers.vdf", '"users"\n{\n"%d"\n{\n"AccountName"\t"acc"\n"PersonaName"\t"Игрок"\n}\n'
          '"%d"\n{\n"AccountName"\t"login2"\n"PersonaName"\t"็"\n}\n}\n' % (c.STEAMID64_BASE + 123456, c.STEAMID64_BASE + 888))
        accs = {a.account_id: a for a in c.find_accounts(self.steam)}
        self.assertEqual(set(accs), {"123456", "555", "888"}); self.assertTrue(accs["123456"].has_cs2)
        self.assertFalse(accs["555"].has_cs2); self.assertEqual(accs["888"].label, "login2 (888)")  # невидимый ник -> логин
        self.assertEqual(c.find_accounts(self.steam)[0].account_id, "123456")  # с конфигом CS2 первыми
        done, warns = self.store.transfer(self.acc, accs["888"], ("binds", "settings"), running_check=NOT_RUNNING)
        self.assertEqual((accs["888"].cfg_dir / "cs2_user_keys_0_slot0.vcfg").read_text(encoding="utf-8"), KEYS)
        self.assertTrue(accs["888"].has_cs2)
    def test_custom_cfg(self):
        for bad in ("", "a b", "../x", "x/y", "con", "a" * 41, "имя"):
            with self.assertRaises(c.ConfigError, msg=bad): c.validate_cfg_name(bad)
        self.assertEqual(c.validate_cfg_name("my_binds.cfg"), "my_binds.cfg")
        t = c.build_cfg("my_binds", {"\\": "toggleconsole", "MOUSE1": "+attack"}, {"sensitivity": "1.2"}, "echo ok")
        self.assertIn("exec my_binds", t); self.assertIn('bind "\\" "toggleconsole"', t); self.assertIn('sensitivity "1.2"', t)
        self.assertIn("echo ok", t); self.assertEqual(c.build_cfg("x", {}, {}), "")
    def test_transfer_includes_video_and_uses_sibling_gpu_keys(self):  # баг: графика не переносилась
        W(self.acc.cfg_dir / "cs2_video.txt", VID_NEW)  # на этом ПК: 0x1002 / 0x2222, оконный режим
        new = c.Account("888", self.steam / "userdata" / "888" / "730" / "local" / "cfg")  # CS2 ещё не запускалась
        done, warns = self.store.transfer(self.acc, new, ("binds", "video"), running_check=NOT_RUNNING)
        self.assertIn("cs2_video.txt", done); self.assertEqual(warns, [])
        self.assertIn('"setting.fullscreen"\t\t"0"', (new.cfg_dir / "cs2_video.txt").read_text(encoding="utf-8"))
        # профиль с другого ПК на аккаунт без видеофайла: настройки из профиля, GPU-ключи соседнего аккаунта
        p = self.store.save("other_pc", self.acc, None, game_names=()); W(p.path / "user/cs2_video.txt", VID_OLD)
        third = c.Account("999", self.steam / "userdata" / "999" / "730" / "local" / "cfg")
        self.store.restore(p, third, None, ("video",), running_check=NOT_RUNNING)
        v = (third.cfg_dir / "cs2_video.txt").read_text(encoding="utf-8")
        self.assertIn('"setting.fullscreen"\t\t"1"', v); self.assertIn('"VendorID"\t\t"0x1002"', v)
    def test_readonly_mode_restored_exactly(self):
        t = self.game / "autoexec.cfg"; os.chmod(t, 0o444); before = stat.S_IMODE(t.stat().st_mode)
        c.write_text_atomic(t, "new"); self.assertEqual(stat.S_IMODE(t.stat().st_mode), before)
        os.chmod(t, 0o666)


if __name__ == "__main__": unittest.main(verbosity=1)
