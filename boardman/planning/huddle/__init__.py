"""Planning engine ported from the standalone ``deepiri-huddle`` project.

Everything in this subpackage originated in the ``huddle`` Python package of the
``deepiri-huddle`` repository and was brought into boardman wholesale in the
"Wave 1 huddle integration" commit. It is kept segmented here so it is obvious
which planning logic was *taken from huddle* versus written natively for
boardman (the meeting-plans REST/service layer that lives one level up in
``boardman.planning``: ``service``, ``context_aggregator``, ``team_config``,
``team_models``).

Boardman's own layer depends on this subpackage; this subpackage should not
reach back into boardman-specific modules except for the team-config helpers it
was rewired to use during integration. See ``ORIGINS.md`` for the file-by-file
mapping back to the huddle sources.
"""
