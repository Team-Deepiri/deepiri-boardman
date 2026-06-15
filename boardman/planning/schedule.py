from boardman.planning.models import TeamMeeting

DEFAULT_TEAM_SCHEDULE: list[TeamMeeting] = [
    TeamMeeting("AI/ML", "Monday", "9:30 PM", "8:30 PM", "7:30 PM", "6:30 PM"),
    TeamMeeting("QA", "Monday", "10:00 PM", "9:00 PM", "8:00 PM", "7:00 PM"),
    TeamMeeting(
        "Frontend + Backend + Infrastructure",
        "Tuesday",
        "9:00 PM",
        "8:00 PM",
        "7:00 PM",
        "6:00 PM",
    ),
]

IT_ATTENDANCE_RULE = "IT may attend any of these meetings, but should attend at least one per week."
