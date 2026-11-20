"""rt4k-serial-deploy.py with every ssh/scp stubbed: the launch guard, the
pushed-md5 baseline in collect, and the STOPPED-deploy warning."""
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "rt4k_serial_deploy", os.path.join(REPO, "scripts", "rt4k-serial-deploy.py"))
deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy)


class Args:
    def __init__(self, **kw):
        self.dry_run, self.limit, self.only_new = False, 0, False
        self.discard_previous, self.partial = False, False
        self.__dict__.update(kw)


class LaunchGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "profile"))
        with open(os.path.join(self.tmp, "profile", "a.rt4"), "wb") as fh:
            fh.write(b"new")
        base = os.path.join(self.tmp, "baseline.txt")
        open(base, "w").close()
        patches = [mock.patch.object(deploy, "MIRROR", self.tmp),
                   mock.patch.object(deploy, "BASELINE", base),
                   mock.patch.object(deploy, "HOST", "root@box")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_guard_script_refuses_without_marker(self):
        script = deploy.prepare_command(discard=False)
        self.assertIn("plan.tsv", script)
        self.assertIn("collected", script)
        self.assertIn("exit 3", script)
        self.assertLess(script.index("exit 3"), script.index("rm -rf"))

    def test_discard_previous_skips_only_the_uncollected_guard(self):
        script = deploy.prepare_command(discard=True)
        self.assertNotIn("collected", script)
        self.assertIn("pgrep", script)          # a RUNNING deploy still refuses
        self.assertLess(script.index("exit 3"), script.index("rm -rf"))

    def test_running_deploy_check_comes_before_the_uncollected_one(self):
        script = deploy.prepare_command(discard=False)
        self.assertLess(script.index("pgrep"), script.index("collected"))

    def test_launch_refuses_on_exit_3_and_never_extracts(self):
        def fake_run(cmd, **kw):
            return mock.Mock(returncode=3 if "plan.tsv" in str(cmd[-1]) else 0)
        with mock.patch.object(deploy.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(deploy.subprocess, "Popen") as popen, \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                deploy.launch(Args())
        self.assertIn("--discard-previous", str(cm.exception))
        popen.assert_not_called()

    def test_launch_stops_on_any_other_ssh_failure(self):
        with mock.patch.object(deploy.subprocess, "run", return_value=mock.Mock(returncode=255)), \
                mock.patch.object(deploy.subprocess, "Popen") as popen, \
                redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                deploy.launch(Args())
        self.assertIn("ssh exit 255", str(cm.exception))
        popen.assert_not_called()


class CollectTest(unittest.TestCase):
    def run_collect(self, plan_rows, result_rows, partial=False):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        base = os.path.join(tmp, "baseline.txt")
        open(base, "w").close()
        texts = {"log": "DONE x=1\n",
                 "plan.tsv": "# h\n" + "".join("%s\t%s\t-\n" % r for r in plan_rows),
                 "result.tsv": "".join("%s\t%s\t-\t%s\n" % r for r in result_rows)}
        written = {}
        with mock.patch.object(deploy, "BACKUPS", os.path.join(tmp, "b")), \
                mock.patch.object(deploy, "BASELINE", base), \
                mock.patch.object(deploy, "remote_text", side_effect=lambda n: texts[n]), \
                mock.patch.object(deploy, "remote_predeploy", lambda path: open(path, "wb").close()), \
                mock.patch.object(deploy, "mark_collected", side_effect=lambda: written.setdefault("marked", True)), \
                mock.patch.object(deploy.manifest, "write", side_effect=lambda p, d: written.update(base=d)):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                deploy.collect(Args(partial=partial))
        return written, out.getvalue(), err.getvalue()

    def test_baseline_is_the_pushed_md5(self):
        # The mirror may have changed since launch; the baseline must hold what was pushed.
        written, _, _ = self.run_collect([("profile/a.rt4", "PUSHED")],
                                         [("profile/a.rt4", "push", "verified")])
        self.assertEqual(written["base"]["profile/a.rt4"], "PUSHED")
        self.assertTrue(written["marked"])

    def test_stopped_deploy_warns(self):
        _, out, err = self.run_collect(
            [("profile/a.rt4", "A"), ("profile/b.rt4", "B")],
            [("profile/a.rt4", "push", "verified")])
        self.assertIn("STOPPED", out + err)
        self.assertIn("1 of 2", out + err)

    def test_complete_deploy_does_not_warn(self):
        _, out, err = self.run_collect([("profile/a.rt4", "A")], [("profile/a.rt4", "push", "verified")])
        self.assertNotIn("STOPPED", out + err)

    def test_partial_collect_leaves_deploy_uncollected(self):
        written, _, _ = self.run_collect([("profile/a.rt4", "A")], [("profile/a.rt4", "push", "verified")],
                                         partial=True)
        self.assertNotIn("marked", written)


if __name__ == "__main__":
    unittest.main()
