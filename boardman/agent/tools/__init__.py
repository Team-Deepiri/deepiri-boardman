from boardman.agent.tools.assignment_tools import assignment_preview_tool
from boardman.agent.tools.github_tools import build_github_tools
from boardman.agent.tools.plaky_tools import build_plaky_tools
from boardman.agent.tools.repo_tools import scan_local_repo_tool, thoughts_tool


def build_all_tools(*, allow_writes: bool):
    return [
        *build_plaky_tools(allow_writes=allow_writes),
        scan_local_repo_tool(),
        thoughts_tool(),
        assignment_preview_tool(),
        *build_github_tools(),
    ]
