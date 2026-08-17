from boardman.agent.tools.assignment_tools import assignment_preview_tool
from boardman.agent.tools.github_tools import build_github_tools
from boardman.agent.tools.plaky_tools import build_plaky_tools
from boardman.agent.tools.repo_tools import scan_local_repo_tool, thoughts_tool

# Tool construction re-runs pydantic schema inference for every tool — ~200-300ms of
# synchronous CPU per turn that also stalls every other in-flight stream on the loop.
# The tools are stateless (per-request state flows through tool_context ContextVars read
# at call time), so two cached lists cover every turn.
_tools_cache: dict[bool, list] = {}


def build_all_tools(*, allow_writes: bool):
    cached = _tools_cache.get(allow_writes)
    if cached is None:
        cached = [
            *build_plaky_tools(allow_writes=allow_writes),
            scan_local_repo_tool(),
            thoughts_tool(),
            assignment_preview_tool(),
            *build_github_tools(),
        ]
        _tools_cache[allow_writes] = cached
    return list(cached)
