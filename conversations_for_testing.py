"""
Pre-defined conversation strings for manual and automated testing of the Boardman agent.
Includes complex multi-step instructions for Plaky and GitHub integration.
"""

# 1. The original complex multi-step prompt
COMPLEX_TASK_CREATION = (
    "Can you create a few tass in the boardman test board, in sprint 2, "
    "create it for the deepiri platform repo, I want david poindexter to be assigned to it, "
    "i want it high priority, it will be a feature, i want it as the next direction "
    "whatever is the next task that should be added to the table (group i mean but you should know that), "
    "as well as can you organize the table (group i mean but you should know that),"
)

# 2. GitHub -> Plaky Sync (The "Linker" scenario)
SYNC_GITHUB_ISSUE_TO_PLAKY = (
    "Check the deepiri/deepiri-boardman repo for any new bug issues opened in the last 24 hours. "
    "For each one, create a corresponding task in the 'Bug Backlog' group of our main board. "
    "Make sure to include the GitHub issue URL in the description and set the priority based on the labels."
)

# 3. The "QA Picker" scenario
QA_ASSIGNMENT_ROUTINE = (
    "Look at all tasks in the 'Ready for QA' group. For any task that doesn't have a QA person assigned, "
    "check the support team roster and pick someone who isn't the lead engineer on the task. "
    "Update the 'QA Member' field in Plaky and leave a comment saying who is handling it."
)

# 4. Complex Filtering & Cleanup
CLEANUP_STALE_TASKS = (
    "Find all tasks in 'Sprint 1' that are still 'In Progress'. "
    "Move them to 'Sprint 2', change their status to 'Todo', and add a comment: "
    "'Automatically rolled over from previous sprint due to incomplete status.'"
)

# 5. Schema Investigation & Dynamic Field Mapping
SCHEMA_MAPPING_TEST = (
    "I want to update the 'Delivery Date' for all tasks labeled 'Urgent'. "
    "First, find out what the exact internal key is for the date field on this board, "
    "then set it to next Friday for those tasks."
)

# 6. Deep Repo Analysis + Task Drafting
REPO_AUDIT_TO_DRAFT = (
    "Scan the current repo for TODO comments in the codebase. "
    "Draft a single Plaky task that summarizes the technical debt you found, "
    "and suggest which group it should live in based on the files involved."
)

# Helper for f-string style templates
def get_task_prompt(repo: str, board: str, group: str) -> str:
    return (
        f"Create a new feature task for the {repo} repository in the {board} board, "
        f"specifically under the {group} group. Set it to high priority."
    )
