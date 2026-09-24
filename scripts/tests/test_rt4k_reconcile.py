import unittest

from rt4k.reconcile import (
    Action,
    CaseCollisionError,
    Decision,
    REPORT_ONLY,
    WRITES_TO_CARD,
    WRITES_TO_MIRROR,
    classify,
    normalize,
    summarize,
)

A, B, C = "a" * 32, "b" * 32, "c" * 32
P = "profile/DV1/SNES.rt4"


def one(card, repo, base):
    """Classify a single path and return its Action."""
    mk = lambda v: {P: v} if v else {}
    return classify(mk(card), mk(repo), mk(base))[0].action


class TestClassify(unittest.TestCase):
    def test_identical_is_in_sync(self):
        self.assertEqual(one(A, A, A), Action.IN_SYNC)

    def test_identical_with_no_baseline_is_still_in_sync(self):
        self.assertEqual(one(A, A, None), Action.IN_SYNC)

    def test_tuned_on_unit_is_adopted(self):
        # This is the SNES bug: card moved, repo still matches what was pushed.
        self.assertEqual(one(B, A, A), Action.ADOPT)

    def test_improved_in_repo_is_pushed(self):
        self.assertEqual(one(A, B, A), Action.PUSH)

    def test_both_changed_is_a_conflict(self):
        self.assertEqual(one(B, C, A), Action.CONFLICT)

    def test_differing_with_no_baseline_is_a_conflict(self):
        # First run: nothing can be classified, so nothing is written.
        self.assertEqual(one(A, B, None), Action.CONFLICT)

    def test_new_on_card_is_adopted(self):
        self.assertEqual(one(A, None, None), Action.ADOPT_NEW)

    def test_new_in_repo_is_pushed(self):
        self.assertEqual(one(None, A, None), Action.PUSH_NEW)

    def test_gone_from_card_is_reported_not_deleted(self):
        self.assertEqual(one(None, A, A), Action.DELETED_ON_CARD)

    def test_gone_from_repo_is_reported_not_deleted(self):
        self.assertEqual(one(A, None, A), Action.DELETED_IN_REPO)

    def test_card_and_repo_agree_is_in_sync_even_with_a_stale_baseline(self):
        # Both live sides already hold the same bytes. Whatever the recorded
        # baseline says, there is nothing left to adopt or push -- the
        # baseline only matters as a tiebreaker when card and repo disagree.
        self.assertEqual(one(A, A, B), Action.IN_SYNC)

    def test_gone_from_both_sides_is_not_reported_at_all(self):
        # A path that only survives in the baseline (deleted from card AND
        # repo) has no discrepancy between the two live sides to flag.
        got = classify({}, {}, {P: A})
        self.assertEqual(got, [])

    def test_deleted_on_card_keeps_the_fact_that_repo_also_moved(self):
        # Repo changed since the baseline AND the card copy is gone. The
        # single action name can't say both, but the full triple of digests
        # on the Decision must still let a reporter notice the repo moved.
        got = classify({}, {P: B}, {P: A})[0]
        self.assertEqual(got.action, Action.DELETED_ON_CARD)
        self.assertEqual(got.repo_md5, B)
        self.assertEqual(got.base_md5, A)
        self.assertNotEqual(got.repo_md5, got.base_md5)

    def test_deleted_in_repo_keeps_the_fact_that_card_also_moved(self):
        # Mirror of the above with card and repo swapped: card changed since
        # the baseline AND the repo copy is gone. The branch never consults
        # c vs b, but the Decision must still carry both digests so a
        # reporter can notice the card moved.
        got = classify({P: B}, {}, {P: A})[0]
        self.assertEqual(got.action, Action.DELETED_IN_REPO)
        self.assertEqual(got.card_md5, B)
        self.assertEqual(got.base_md5, A)
        self.assertNotEqual(got.card_md5, got.base_md5)

    def test_decision_path_is_never_none(self):
        for card, repo, base in [({P: A}, {}, {}), ({}, {P: A}, {}),
                                  ({}, {P: A}, {P: A}), ({P: A}, {}, {P: A})]:
            for d in classify(card, repo, base):
                self.assertIsNotNone(d.path)


class TestCaseInsensitivity(unittest.TestCase):
    def test_card_is_fat_so_case_differences_are_the_same_file(self):
        # NGPC.rt4 vs ngpc.rt4 collide on the card; they must not both appear.
        got = classify({"profile/DV1/NGPC.rt4": A}, {"profile/DV1/ngpc.rt4": A}, {})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].action, Action.IN_SYNC)

    def test_normalize_casefolds_and_keeps_separators(self):
        self.assertEqual(normalize("Profile/DV1/SNES.rt4"), "profile/dv1/snes.rt4")

    def test_repo_side_collision_raises_instead_of_silently_dropping_one(self):
        # Real, live case: an arcade setname's profile and a core's own
        # profile can fold to the same card filename. If both exist as
        # distinct entries on one side, silently keeping only one (dict
        # last-wins) could push the wrong bytes under the surviving name --
        # so this must halt loudly, not pick arbitrarily.
        with self.assertRaises(CaseCollisionError):
            classify({}, {"profile/DV1/NGPC.rt4": A, "profile/DV1/ngpc.rt4": B}, {})

    def test_card_side_collision_raises(self):
        with self.assertRaises(CaseCollisionError):
            classify({"NGPC.rt4": A, "ngpc.rt4": B}, {}, {})

    def test_baseline_side_collision_raises(self):
        with self.assertRaises(CaseCollisionError):
            classify({}, {}, {"NGPC.rt4": A, "ngpc.rt4": B})

    def test_collision_is_detected_amid_unrelated_clean_entries(self):
        # Detection must not depend on the map containing only the
        # colliding pair -- it has to find the collision inside a normal,
        # mostly-fine set of paths.
        repo = {
            "profile/DV1/SNES.rt4": A,
            "profile/DV1/Genesis.rt4": B,
            "profile/DV1/NGPC.rt4": A,
            "profile/DV1/ngpc.rt4": B,
            "profile/DV1/PSX.rt4": C,
        }
        with self.assertRaises(CaseCollisionError):
            classify({}, repo, {})


class TestActionGroups(unittest.TestCase):
    def test_writes_report_only_and_conflict_partition_all_actions(self):
        # The CLI (Task 7) sorts every decision into exactly one of these
        # four buckets: copy card->mirror, push mirror->card, halt, or
        # report-only. If someone adds a new Action and forgets to put it
        # in one of the tuples below, this must fail -- not silently drop
        # the new action into "nothing happens."
        groups = [WRITES_TO_MIRROR, WRITES_TO_CARD, REPORT_ONLY, (Action.CONFLICT,)]

        for action in Action:
            memberships = [action in g for g in groups]
            self.assertEqual(
                sum(memberships), 1,
                f"{action} must appear in exactly one group, appeared in {sum(memberships)}",
            )

        self.assertEqual(
            set().union(*groups), set(Action),
            "every Action must be covered by some group",
        )


class TestOrderingAndSummary(unittest.TestCase):
    def test_results_are_sorted_by_path(self):
        got = classify({"b.rt4": A, "a.rt4": A}, {"b.rt4": A, "a.rt4": A}, {})
        self.assertEqual([d.path for d in got], ["a.rt4", "b.rt4"])

    def test_display_path_prefers_the_card_spelling(self):
        got = classify({"NGPC.rt4": A}, {"ngpc.rt4": A}, {})
        self.assertEqual(got[0].path, "NGPC.rt4")

    def test_summarize_counts_each_action(self):
        got = classify({"a.rt4": A, "b.rt4": B}, {"a.rt4": A, "b.rt4": A}, {"b.rt4": A})
        # a.rt4 -> IN_SYNC, b.rt4 -> card moved from baseline while repo held
        # still, i.e. ADOPT.
        self.assertEqual(summarize(got), {Action.IN_SYNC: 1, Action.ADOPT: 1})

    def test_decision_fields_are_populated(self):
        got = classify({P: A}, {P: B}, {P: A})[0]
        self.assertEqual(
            got, Decision(P, Action.PUSH, card_md5=A, repo_md5=B, base_md5=A)
        )


if __name__ == "__main__":
    unittest.main()
