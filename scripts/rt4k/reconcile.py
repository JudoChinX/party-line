"""Three-way classification of card vs repo mirror vs deployed baseline.

Pure: takes three {path: md5} maps and returns decisions. No file or network
access, so every row of the contract is unit-testable.

The card is FAT and case-insensitive, so paths are compared casefolded. The
card's own spelling is kept for display when both sides have the file.

Deletions are never propagated in either direction -- a mirror-driven delete
could wipe a profile the RT4K still needs, and the scaler does not fall back
when a named profile goes missing; it keeps whatever was last loaded, which
presents to the user as a black screen. DELETED_ON_CARD / DELETED_IN_REPO are
reported only.
"""
from dataclasses import dataclass
from enum import Enum


class Action(Enum):
    IN_SYNC = "in-sync"
    ADOPT = "adopt"                      # tuned on the unit -> mirror takes it
    PUSH = "push"                        # improved in the repo -> card takes it
    CONFLICT = "conflict"                # both moved, or no baseline to judge by
    ADOPT_NEW = "adopt-new"
    PUSH_NEW = "push-new"
    DELETED_ON_CARD = "deleted-on-card"  # reported, never applied
    DELETED_IN_REPO = "deleted-in-repo"  # reported, never applied


WRITES_TO_MIRROR = (Action.ADOPT, Action.ADOPT_NEW)
WRITES_TO_CARD = (Action.PUSH, Action.PUSH_NEW)
REPORT_ONLY = (Action.IN_SYNC, Action.DELETED_ON_CARD, Action.DELETED_IN_REPO)


class CaseCollisionError(ValueError):
    """Two paths on one side fold to the same name on a case-insensitive card.

    This is not hypothetical: the live profile set has at least one arcade
    setname that collides with a core name (e.g. an arcade "ngpc" MRA next
    to the NGPC core's own profile). If both existed as distinct entries on
    one side, a dict keyed by the casefolded name would silently keep
    whichever one the input's iteration order happened to visit last -- and
    since this function decides what gets overwritten, an arbitrary silent
    choice there could push or adopt the wrong profile's bytes under the
    other one's name. Raising and halting is the only safe response; the fix
    is for the caller to rename one of the colliding files before syncing.
    """


@dataclass(frozen=True)
class Decision:
    path: str
    action: Action
    card_md5: str
    repo_md5: str
    base_md5: str


def normalize(path):
    return path.casefold()


def _index(mapping, side):
    out = {}
    for path, digest in mapping.items():
        key = normalize(path)
        if key in out:
            other_path, _ = out[key]
            raise CaseCollisionError(
                f"{side}: {other_path!r} and {path!r} both fold to {key!r} on a "
                "case-insensitive card; rename one before syncing"
            )
        out[key] = (path, digest)
    return out


def classify(card, repo, base):
    ci = _index(card, "card")
    ri = _index(repo, "repo")
    bi = _index(base, "baseline")

    decisions = []
    # Only paths present on the card or in the repo are ever reported. A path
    # that survives solely in the baseline (gone from both card and repo) has
    # no discrepancy between the two live sides to flag, so it is dropped --
    # and because every key here comes from ci or ri, `cpath or rpath` below
    # is never None: Decision.path always has a real spelling to show.
    for key in sorted(set(ci) | set(ri)):
        cpath, c = ci.get(key, (None, None))
        rpath, r = ri.get(key, (None, None))
        _, b = bi.get(key, (None, None))

        if c is not None and r is not None:
            if c == r:
                # Card and repo already agree, regardless of what the
                # baseline says. A stale/mismatched baseline here means
                # nothing needs to be written on either side -- baseline
                # drift only matters as a tiebreaker when the live sides
                # disagree, so it's not surfaced as its own action.
                action = Action.IN_SYNC
            elif b is None:
                action = Action.CONFLICT
            elif r == b:
                action = Action.ADOPT
            elif c == b:
                action = Action.PUSH
            else:
                action = Action.CONFLICT
        elif c is not None:
            # Missing from the repo. If it was never in the baseline either,
            # it's brand new on the card. If it was in the baseline, the
            # repo copy is gone -- report only; card_md5/repo_md5/base_md5
            # on the Decision still capture whether the surviving side has
            # itself moved since the baseline, so no information is lost by
            # collapsing this to a single action name.
            action = Action.DELETED_IN_REPO if b is not None else Action.ADOPT_NEW
        else:
            action = Action.DELETED_ON_CARD if b is not None else Action.PUSH_NEW

        decisions.append(Decision(cpath or rpath, action, c, r, b))

    return decisions


def summarize(decisions):
    counts = {}
    for d in decisions:
        counts[d.action] = counts.get(d.action, 0) + 1
    return counts
