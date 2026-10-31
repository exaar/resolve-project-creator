import sys, pathlib, tempfile, unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from rpc import diskdb


class ParseTests(unittest.TestCase):
    def test_dblist_and_active(self):
        text = ("Local Database:C\\Users\\someone\\Resolve Disk Database::::DISK\r\n"
                "Work:X\\Library\\Resolve Project Library:*:::DISK\r\n"
                "net1:10.0.0.5:user:secret:studio:QPSQL\r\n")
        entries = diskdb.parse_dblist(text)
        self.assertEqual([e.kind for e in entries], ["disk", "disk", "network"])
        self.assertEqual(str(entries[1].path), "X:\\Library\\Resolve Project Library")
        self.assertEqual(entries[2].name, "studio")
        self.assertNotIn("secret", repr(entries[2]))
        self.assertEqual(diskdb.parse_activedb("cloud:abc\r\ndisk*:Work\r\nnetwork:net1\r\n"), ("disk", "Work"))
        self.assertEqual(diskdb.parse_activedb("disk:Work\nnetwork*:net1\n"), ("network", "net1"))

    def test_names(self):
        for bad in ["a:b", "a/b", "a\\b", " a", "a ", "a.", "CON", "", "x?"]:
            self.assertIsNotNone(diskdb.name_problem(bad), bad)
        for ok in ["Project 1", "Client_v2", "Проект", "a.b"]:
            self.assertIsNone(diskdb.name_problem(ok), ok)

    def test_snapshot(self):
        with tempfile.TemporaryDirectory() as t:
            g = pathlib.Path(t) / "Resolve Projects" / "Users" / "guest"
            for p in ["Projects/Alpha", "Projects/Sub/Beta", "Projects/Sub/Deeper/Gamma"]:
                (g / p).mkdir(parents=True)
                (g / p / "Project.db").write_bytes(b"")
            (g / "Projects/EmptyFolder").mkdir()
            (g / "Configs").mkdir()
            for n in ["User Default Config", "HDR", "basic"]:
                (g / "Configs" / f"{n}.xml").write_text("<x/>")
            snap = diskdb.snapshot(diskdb.DbEntry("k", "k", "disk", pathlib.Path(t)))
            self.assertEqual(snap.presets, ["System Config", "guest default config", "basic", "HDR"])
            self.assertEqual(snap.projects[""], {"alpha"})
            self.assertEqual(snap.projects["Sub"], {"beta"})
            self.assertEqual(snap.projects["Sub/Deeper"], {"gamma"})
            self.assertIn("EmptyFolder", snap.projects)


if __name__ == "__main__":
    unittest.main()
