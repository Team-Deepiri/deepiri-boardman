from dataclasses import dataclass

from boardman.services.pr_task_linking import TaskCandidate, score_candidate


@dataclass
class TestCase:
    name: str
    pr_title: str
    pr_body: str
    head_ref: str
    tasks: list[dict]
    expected_task_id: str
    pr_author: dict = None


def run_benchmark():
    test_cases = [
        TestCase(
            name="Exact Title Match",
            pr_title="Update README documentation",
            pr_body="Improving the docs",
            head_ref="main",
            tasks=[
                {"id": "T1", "title": "Update README documentation", "description": ""},
                {"id": "T2", "title": "Fix bug in auth", "description": ""},
            ],
            expected_task_id="T1",
        ),
        TestCase(
            name="Branch Name Number Match",
            pr_title="Fixing login",
            pr_body="Login was broken",
            head_ref="fix/123-login",
            tasks=[
                {"id": "T1", "title": "Broken login", "description": "See issue #123"},
                {"id": "T2", "title": "Other task", "description": "No ref"},
            ],
            expected_task_id="T1",
        ),
        TestCase(
            name="Assignee Match Boost",
            pr_title="Refactor database",
            pr_body="Cleanup",
            head_ref="refactor-db",
            pr_author={"login": "davidp", "name": "David Poindexter", "email": "david@deepiri.ai"},
            tasks=[
                {
                    "id": "T1",
                    "title": "Database refactor",
                    "description": "",
                    "assignee_name": "David Poindexter",
                    "assignee_email": "david@deepiri.ai",
                },
                {
                    "id": "T2",
                    "title": "Database refactor",
                    "description": "",
                    "assignee_name": "Someone Else",
                },
            ],
            expected_task_id="T1",
        ),
        TestCase(
            name="Fuzzy Title Match",
            pr_title="Add new user dashboard",
            pr_body="",
            head_ref="feature/dashboard",
            tasks=[
                {
                    "id": "T1",
                    "title": "User Dashboard implementation",
                    "description": "Add the dashboard",
                },
                {"id": "T2", "title": "Settings page", "description": ""},
            ],
            expected_task_id="T1",
        ),
        TestCase(
            name="Near Exact Title Match Boost",
            pr_title="Add new user dashboard",
            pr_body="",
            head_ref="main",
            tasks=[
                {"id": "T1", "title": "Add new user dashboard", "description": ""},
                {"id": "T2", "title": "Dashboard work", "description": ""},
            ],
            expected_task_id="T1",
        ),
        TestCase(
            name="Active Status Boost",
            pr_title="Fix auth",
            pr_body="",
            head_ref="fix-auth",
            tasks=[
                {"id": "T1", "title": "Fix auth", "status": "In Progress"},
                {"id": "T2", "title": "Fix auth", "status": "Backlog"},
            ],
            expected_task_id="T1",
        ),
        TestCase(
            name="Done Status Penalty",
            pr_title="Fix auth",
            pr_body="",
            head_ref="fix-auth",
            tasks=[
                {"id": "T1", "title": "Fix auth", "status": "Done"},
                {"id": "T2", "title": "Fix auth", "status": "To Do"},
            ],
            expected_task_id="T2",
        ),
        TestCase(
            name="Branch Token Match Boost",
            pr_title="Some PR",
            pr_body="",
            head_ref="feat/user-auth-logic",
            tasks=[
                {"id": "T1", "title": "Implement User Auth Logic", "description": ""},
                {"id": "T2", "title": "Random task", "description": ""},
            ],
            expected_task_id="T1",
        ),
    ]

    for case in test_cases:
        print(f"\n--- Running Test: {case.name} ---")
        scored_results = []

        # We need to simulate the set of issue numbers from PR
        from boardman.services.pr_task_linking import referenced_issue_numbers

        ref_issues = referenced_issue_numbers(
            repo_full="deepiri/test",
            pr_title=case.pr_title,
            pr_body=case.pr_body,
            head_ref=case.head_ref,
        )

        for task_data in case.tasks:
            cand = TaskCandidate(
                task_id=task_data["id"],
                title=task_data["title"],
                description=task_data.get("description", ""),
                status=task_data.get("status"),
                issue_numbers=referenced_issue_numbers(
                    repo_full="deepiri/test",
                    pr_title=task_data["title"],
                    pr_body=task_data.get("description", ""),
                    head_ref="",
                ),
                assignee_name=task_data.get("assignee_name"),
                assignee_email=task_data.get("assignee_email"),
                assignee_login=task_data.get("assignee_login"),
            )

            scored = score_candidate(
                cand,
                ref_issues=ref_issues,
                pr_title=case.pr_title,
                pr_body=case.pr_body,
                repo_full="deepiri/test",
                pr_number=1,
                session_penalty=False,
                head_ref=case.head_ref,
                pr_author_login=case.pr_author.get("login") if case.pr_author else None,
                pr_author_name=case.pr_author.get("name") if case.pr_author else None,
                pr_author_email=case.pr_author.get("email") if case.pr_author else None,
            )
            scored_results.append(scored)

        scored_results.sort(key=lambda x: x.score, reverse=True)

        for s in scored_results:
            winner_mark = " [EXPECTED WINNER]" if s.task_id == case.expected_task_id else ""
            print(f"Task {s.task_id}: Score={s.score:.2f}{winner_mark}")
            print(f"  Breakdown: {s.breakdown}")

        best = scored_results[0]
        if best.task_id == case.expected_task_id:
            print("SUCCESS: Correct task matched.")
        else:
            print(f"FAILURE: Matched {best.task_id} instead of {case.expected_task_id}")


if __name__ == "__main__":
    run_benchmark()
