from boardman.agent.tools.github_tools import github_list_open_issues_tool
from boardman.agent.tools.plaky_tools import build_plaky_tools
from boardman.agent.tools.repo_tools import scan_local_repo_tool


def build_all_tools(*, allow_writes: bool):
    return [
        *build_plaky_tools(allow_writes=allow_writes),
        scan_local_repo_tool(),
        github_list_open_issues_tool(),
    ]
