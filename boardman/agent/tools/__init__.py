from boardman.agent.tools.assignment_tools import assignment_preview_tool
from boardman.agent.tools.cognition_tools import planning_candidates_tool
from boardman.agent.tools.github_tools import build_github_tools
from boardman.agent.tools.plaky_tools import build_plaky_tools
from boardman.agent.tools.repo_tools import scan_local_repo_tool, thoughts_tool

# Tool construction re-runs pydantic schema inference for every tool — ~200-300ms of
# synchronous CPU per turn that also stalls every other in-flight stream on the loop.
# The tools are stateless (per-request state flows through tool_context ContextVars read
# at call time), so two cached lists cover every turn.
#
# ONE cache, keyed on EVERY variant the list can take. There used to be a second cache in
# runner.py holding the timing-wrapped copies, which meant two layers memoising the same
# tools and two places to get the key wrong (Sorge review, PR #88). The timing wrapper is
# just another variant, so it lives in the key.
#
# The key is load bearing. Anything that would vary the tool list by something else — a
# per-intent subset, a per-repo filter, a feature flag — must widen this key FIRST.
# Storing a narrowed list under an existing key would hand write tools to a read-only
# turn, which is the one mistake this cache can make. Tool definitions are static per
# process, so a restart is the invalidation.
_tools_cache: dict[tuple[bool, bool], list] = {}


def build_all_tools(*, allow_writes: bool, timed: bool = False):
    """The agent's tool list. ``timed`` wraps each tool so its wall time is logged."""
    key = (bool(allow_writes), bool(timed))
    cached = _tools_cache.get(key)
    if cached is None:
        cached = [
            *build_plaky_tools(allow_writes=allow_writes),
            scan_local_repo_tool(),
            thoughts_tool(),
            assignment_preview_tool(),
            planning_candidates_tool(),
            *build_github_tools(),
        ]
        if timed:
            from boardman.agent.tool_timing import with_timing

            cached = with_timing(cached)
        _tools_cache[key] = cached
    return list(cached)
