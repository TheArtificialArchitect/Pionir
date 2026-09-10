"""Link expansion and the shared lessons namespace / recall-before-act hook."""

import unittest

from pionir.cortex import LESSONS_NAMESPACE, Cortex


def _mem():
    return Cortex(":memory:")


class LinkExpansionTests(unittest.TestCase):
    def test_expansion_pulls_one_linked_hop(self) -> None:
        c = _mem()
        c.remember("fact", "the deploy uses wrangler to publish", slug="deploy", links=["rollback"])
        c.remember("fact", "undo it by reverting to the previous version", slug="rollback")
        direct = c.recall("how does the deploy publish")
        self.assertNotIn("rollback", [m.slug for m in direct])   # shares no word with the query
        expanded = c.recall("how does the deploy publish", expand_links=True)
        slugs = [m.slug for m in expanded]
        self.assertIn("rollback", slugs)
        linked = next(m for m in expanded if m.slug == "rollback")
        self.assertEqual(linked.via, "deploy")                   # provenance recorded

    def test_expansion_never_crosses_a_namespace(self) -> None:
        # A hit in namespace A links to a slug that exists in namespace B; the
        # scoped recall must NOT pull B's memory in - that would leak private memory.
        c = _mem()
        c.remember("fact", "public thing that mentions the secret", slug="pub", links=["sec"],
                   namespace="a")
        c.remember("fact", "a private secret in another bot's namespace", slug="sec",
                   namespace="b")
        hits = c.recall("public thing", namespace="a", expand_links=True)
        self.assertNotIn("sec", [m.slug for m in hits])


class LessonsTests(unittest.TestCase):
    def test_a_lesson_lands_in_the_shared_namespace_at_high_salience(self) -> None:
        c = _mem()
        mid = c.record_lesson("never git add . — commit narrowly, naming paths")
        got = c.get(mid)
        self.assertEqual(got.namespace, LESSONS_NAMESPACE)
        self.assertEqual(got.kind, "lesson")
        self.assertEqual(got.salience, 8.0)

    def test_lessons_for_recalls_before_acting(self) -> None:
        c = _mem()
        c.record_lesson("a stale feature branch deployed to prod reverted 10 commits; "
                        "check git log --oneline main ^HEAD before deploying", slug="branch-check")
        c.record_lesson("wrangler deploys are manual and go to the main branch", links=["branch-check"])
        # ordinary conversation memory must not drown the lessons out - they live apart
        c.remember("thought", "the coffee is cold", namespace="chat")
        hits = c.lessons_for("about to deploy the site with wrangler")
        self.assertTrue(hits)
        texts = " ".join(m.text for m in hits)
        self.assertIn("branch", texts)
        # link expansion brought in the linked lesson too
        self.assertTrue(any(m.via for m in hits))

    def test_lessons_are_scoped_out_of_ordinary_recall(self) -> None:
        # A bot recalling its own conversation must not get the lessons namespace
        # unless it asks for it.
        c = _mem()
        c.record_lesson("a lesson about deploys")
        c.remember("fact", "a deploy happened on Friday", namespace="chat")
        chat_only = c.recall("deploy", namespace="chat")
        self.assertTrue(all(m.namespace == "chat" for m in chat_only))


if __name__ == "__main__":
    unittest.main()
