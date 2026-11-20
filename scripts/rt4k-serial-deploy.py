#!/usr/bin/env python3
"""Push profiles from a local folder to the RT4K's SD card over serial -- no card trip.

The MIRROR is a local folder laid out like the root of the RT4K's SD card:
MIRROR/profile/DV1/SNES.rt4 lands on the card as /profile/DV1/SNES.rt4. Only
MIRROR/profile/ is deployed.

Every file is classified card-vs-mirror-vs-deployed-baseline before it is
written (scripts/rt4k/reconcile.py rules, re-implemented byte-for-byte in
rt4k-serial.py's decide() and pinned by a test). Only push/push-new outcomes
are written, every overwritten card copy is kept, and every write is read back
and hashed. It never adopts card changes into the mirror, and it never touches
mask/ or csc/: conflicts and unit-tuned profiles are reported, not written.

The baseline is MIRROR/.deployed-manifest.txt: what the card held after the
last verified deploy. **On a first run there is no baseline**, so any card file
that differs from the mirror is a conflict and is left alone. Only files the
card does not have yet are written. That is deliberate. A profile you tuned on
the unit is never overwritten because a folder on your PC disagrees with it.

Two phases, because a full deploy takes ~1 s per file and runs detached on
the MiSTer:

    scripts/rt4k-serial-deploy.py --mirror DIR launch [--dry-run] [--limit N] [--only-new] [--discard-previous]
    scripts/rt4k-serial-deploy.py --mirror DIR status
    scripts/rt4k-serial-deploy.py --mirror DIR collect [--partial]

launch  refuses while /tmp/rt4k-deploy on the MiSTer holds a deploy that is
        running (always) or whose results were never collected (plan.tsv
        present, no `collected` marker; --discard-previous overrides). It
        checks inside the same ssh that would otherwise rm the folder. Then it
        computes the pending set (MIRROR/profile/ files whose md5 differs from
        the baseline), stages them plus plan.tsv on the MiSTer's tmpfs, and
        starts `rt4k-serial.py deploy` there under nohup.
status  tails the log.
collect fetches result.tsv and the pre-overwrite copies, stores them under
        the backups folder (default MIRROR/.rt4k-backups), and updates the
        baseline for every verified push, using the md5 that was PUSHED
        (plan.tsv on the MiSTer), not the current mirror. It warns when there
        are fewer result rows than plan rows (a STOPPED deploy), and marks a
        full collect `collected` on the MiSTer (--partial does not).

--mirror can also come from RT4K_MIRROR, and the MiSTer from RT4K_HOST.
"""
import argparse
import datetime
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from rt4k import manifest  # noqa: E402

HOST = os.environ.get("RT4K_HOST", "")
TOOL = os.path.join(HERE, "rt4k-serial.py")
WORKDIR = "/tmp/rt4k-deploy"
# Set by main() from --mirror / --backups.
MIRROR = BASELINE = BACKUPS = None


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def ssh(command, **kw):
    return sh(["ssh", "-o", "BatchMode=yes", HOST, command], **kw)


def prepare_command(discard):
    """The one ssh command that readies WORKDIR; exits 3 (touching nothing) when
    an earlier deploy is still running, or was never collected unless discard.
    A RUNNING deploy always refuses: rm -rf would not stop it (it works from paths),
    and the new one could not take the serial lock anyway."""
    running = ("if pgrep -f '[r]t4k-serial.py deploy' >/dev/null; then "
               "echo 'a deploy is RUNNING in {w}; wait for it to finish'; exit 3; fi; ").format(w=WORKDIR)
    uncollected = "" if discard else (
        "if [ -e {w}/plan.tsv ] && [ ! -e {w}/collected ]; then "
        "echo 'an earlier deploy in {w} was never collected'; exit 3; fi; ").format(w=WORKDIR)
    return running + uncollected + "rm -rf %s && mkdir -p %s" % (WORKDIR, WORKDIR)


def remote_text(name):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", HOST, "cat %s/%s" % (WORKDIR, name)],
                          capture_output=True, text=True, check=True).stdout


def remote_predeploy(path):
    with open(path, "wb") as fh:
        sh(["ssh", "-o", "BatchMode=yes", HOST,
            "cd %s && [ -d before ] && tar czf - before || tar czf - --files-from /dev/null" % WORKDIR], stdout=fh)


def mark_collected():
    ssh("touch %s/collected" % WORKDIR)


def pending(only_new=False):
    base = manifest.read(BASELINE)
    out = []
    for dirpath, _, filenames in os.walk(os.path.join(MIRROR, "profile")):
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, MIRROR).replace(os.sep, "/")
            with open(full, "rb") as fh:
                md5 = hashlib.md5(fh.read()).hexdigest()
            b = base.get(rel)
            if b == md5 or (only_new and b is not None):
                continue
            out.append((rel, md5, b))
    return sorted(out)


def launch(args):
    plan = pending(args.only_new)
    if args.limit:
        plan = plan[:args.limit]
    bad = [rel for rel, _, _ in plan if " " in rel or "\t" in rel]
    if bad:
        sys.exit("refusing: %d path(s) with spaces -- the firmware's get/put tokenise on them: %s" % (len(bad), bad[:3]))
    print("pending: %d file(s) (%d new, %d changed)" % (
        len(plan), sum(1 for _, _, b in plan if b is None), sum(1 for _, _, b in plan if b is not None)))
    if args.dry_run or not plan:
        for rel, md5, b in plan[:20]:
            print("  %s %s" % ("new    " if b is None else "changed", rel))
        return
    stage = tempfile.mkdtemp(prefix="rt4k-deploy-")
    try:
        with open(os.path.join(stage, "plan.tsv"), "w", encoding="utf-8") as fh:
            fh.write("# path\trepo md5\tbaseline md5\n")
            for rel, md5, b in plan:
                fh.write("%s\t%s\t%s\n" % (rel, md5, b or "-"))
        for rel, _, _ in plan:
            dest = os.path.join(stage, "files", rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(os.path.join(MIRROR, rel), dest)
        shutil.copyfile(TOOL, os.path.join(stage, "rt4k-serial.py"))
        prep = subprocess.run(["ssh", "-o", "BatchMode=yes", HOST, prepare_command(args.discard_previous)])
        if prep.returncode == 3:
            sys.exit("refusing: the box holds a running or uncollected deploy (see above). Running: wait "
                     "for it. Uncollected: run `collect` first; after a `collect --partial` (a deploy that "
                     "died before DONE), pass --discard-previous to throw the rest away")
        if prep.returncode != 0:
            sys.exit("could not ready %s on %s (ssh exit %d)" % (WORKDIR, HOST, prep.returncode))
        tar = subprocess.Popen(["tar", "cf", "-", "-C", stage, "."], stdout=subprocess.PIPE)
        sh(["ssh", "-o", "BatchMode=yes", HOST, "tar xf - -C %s" % WORKDIR], stdin=tar.stdout)
        tar.wait()
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    ssh("cd %s && RT4K_LOCAL=1 nohup python3 rt4k-serial.py deploy %s > log 2>&1 &" % (WORKDIR, WORKDIR))
    print("launched on %s; follow with: %s status" % (HOST, sys.argv[0]))


def status(args):
    ssh("tail -n 5 %s/log; wc -l < %s/result.tsv 2>/dev/null" % (WORKDIR, WORKDIR))


def collect(args):
    log = remote_text("log")
    if "DONE " not in log and not args.partial:
        sys.exit("deploy has not finished (no DONE line in the log); use --partial to collect anyway")
    stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H%M%S")
    os.makedirs(BACKUPS, exist_ok=True)
    result_path = os.path.join(BACKUPS, "rt4k-serial-%s-result.tsv" % stamp)
    plan_text = remote_text("plan.tsv")
    with open(result_path, "w", encoding="utf-8") as fh:
        fh.write(remote_text("result.tsv"))
    tgz = os.path.join(BACKUPS, "rt4k-serial-%s-predeploy.tgz" % stamp)
    remote_predeploy(tgz)
    pushed = {}
    for line in plan_text.splitlines():
        if line and not line.startswith("#"):
            rel, md5, _base = line.split("\t")
            pushed[rel] = md5
    with open(result_path, encoding="utf-8") as fh:
        rows = [line.rstrip("\n").split("\t") for line in fh if line.strip()]
    counts = {}
    verified = []
    for rel, action, _card, status_ in rows:
        key = action if action not in ("push", "push-new") else "%s/%s" % (action, status_)
        counts[key] = counts.get(key, 0) + 1
        # in-sync means the card already holds the repo's bytes (typically a
        # push whose read-back verify failed transiently on an earlier run),
        # so it is just as baseline-worthy as a verified push.
        if status_ == "verified" or action == "in-sync":
            verified.append(rel)
    for k, n in sorted(counts.items()):
        print("  %-24s %d" % (k, n))
    if len(rows) < len(pushed):
        print("WARNING: STOPPED deploy -- %d of %d planned file(s) have a result row; the rest were never "
              "attempted (stopped after consecutive errors, or still running). Re-launch to finish."
              % (len(rows), len(pushed)))
    for rel, action, _card, status_ in rows:
        if action in ("conflict", "adopt", "deleted-on-card", "error") or status_ == "VERIFY-FAILED":
            print("  %-16s %s %s" % (action, rel, status_))
    if verified:
        base = manifest.read(BASELINE)
        for rel in verified:
            base[rel] = pushed[rel]
        manifest.write(BASELINE, base)
        print("baseline updated for %d verified push(es)" % len(verified))
    if not args.partial:
        mark_collected()
    print("results: %s\nbackup:  %s" % (result_path, tgz))


def main():
    global MIRROR, BASELINE, BACKUPS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mirror", default=os.environ.get("RT4K_MIRROR"),
                    help="local folder laid out like the RT4K card root (holds profile/)")
    ap.add_argument("--backups", help="where collect stores results and pre-overwrite copies "
                                      "(default MIRROR/.rt4k-backups)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("launch")
    l.add_argument("--dry-run", action="store_true")
    l.add_argument("--limit", type=int, default=0)
    l.add_argument("--only-new", action="store_true")
    l.add_argument("--discard-previous", action="store_true",
                   help="discard an uncollected (not a running) deploy on the box")
    sub.add_parser("status")
    c = sub.add_parser("collect")
    c.add_argument("--partial", action="store_true")
    args = ap.parse_args()
    if not args.mirror or not os.path.isdir(os.path.join(args.mirror, "profile")):
        ap.error("--mirror (or RT4K_MIRROR) must name a folder that contains profile/")
    if not HOST:
        ap.error("RT4K_HOST is unset -- export RT4K_HOST=root@<your-mister>")
    MIRROR = os.path.abspath(args.mirror)
    BASELINE = os.path.join(MIRROR, ".deployed-manifest.txt")
    BACKUPS = os.path.abspath(args.backups) if args.backups else os.path.join(MIRROR, ".rt4k-backups")
    {"launch": launch, "status": status, "collect": collect}[args.cmd](args)


if __name__ == "__main__":
    main()
