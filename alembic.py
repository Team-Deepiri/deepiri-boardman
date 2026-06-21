from alembic import command
from alembic.config import Config


def get_revision() -> str:
    return "001_initial"


def get_down_revision() -> str | None:
    return None


def upgrade():
    config = Config("/home/joeblack/Documents/Deepiri/deepiri-boardman/alembic.ini")
    command.upgrade(config, "head")


def downgrade():
    config = Config("/home/joeblack/Documents/Deepiri/deepiri-boardman/alembic.ini")
    command.downgrade(config, "-1")


def generate_migration(message: str = "auto migration"):
    config = Config("/home/joeblack/Documents/Deepiri/deepiri-boardman/alembic.ini")
    command.revision(config, message=message, autogenerate=True)
