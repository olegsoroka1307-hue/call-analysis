"""provenance на рядках + provider namespace для ідентифікаторів

Дві незалежні проблеми, які закриваються разом, бо обидві — про те, що
рядок у базі має сам себе пояснювати.

**1. Provenance (ТЗ §1).** Раніше походження даних жило лише на fixtures, а
API рахував прапорець «синтетика» глобально по всій базі. У змішаній
live/replay базі це давало неправильну відповідь: один старий replay-матч
робив «синтетичними» всі відповіді, включно з побудованими на живих цінах.
Тепер `data_source` стоїть на odds_snapshots і model_runs, і кожна відповідь
визначає походження за тими рядками, які вона реально повернула.

**2. Provider namespace (ТЗ §6).** `provider_fixture_id`, `leagues.provider_id`
і `teams.name` були унікальні глобально. Це працює рівно доти, доки провайдер
один. Два провайдери — і матч #12345 у The Odds API злипається з матчем #12345
в API-Football, а «Arsenal» з різних джерел стає однією командою. Унікальність
переїжджає на композитний ключ (provider, ...).

Revision ID: 0003_provenance_ns
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_provenance_ns"
down_revision = "0002_append_only"
branch_labels = None
depends_on = None

DEFAULT_PROVIDER = "the_odds_api"


def upgrade() -> None:
    # --- 1. provenance ------------------------------------------------------
    # Тригер append-only забороняє UPDATE на odds_snapshots, тому колонка
    # додається одразу зі server_default: наявні рядки заповнює сам ALTER,
    # без UPDATE, який би впав.
    op.add_column(
        "odds_snapshots",
        sa.Column("data_source", sa.String(32), nullable=False, server_default="LIVE"),
    )
    op.add_column(
        "model_runs",
        sa.Column("data_source", sa.String(32), nullable=False, server_default="LIVE"),
    )
    # Історичні рядки, створені replay-прогоном, успадковують позначку матчу.
    op.execute(
        """
        UPDATE model_runs mr
        SET data_source = f.data_source
        FROM fixtures f
        WHERE f.id = mr.fixture_id AND f.data_source IS NOT NULL
        """
    )

    # --- 2. provider namespace ---------------------------------------------
    for table in ("leagues", "teams", "fixtures"):
        op.add_column(
            table,
            sa.Column(
                "provider", sa.String(32), nullable=False, server_default=DEFAULT_PROVIDER
            ),
        )

    op.drop_constraint("uq_leagues_provider_id", "leagues", type_="unique")
    op.create_unique_constraint(
        "uq_leagues_provider_provider_id", "leagues", ["provider", "provider_id"]
    )

    op.drop_constraint("uq_teams_name", "teams", type_="unique")
    op.create_unique_constraint("uq_teams_provider_name", "teams", ["provider", "name"])

    op.drop_constraint("uq_fixtures_provider_fixture_id", "fixtures", type_="unique")
    op.create_unique_constraint(
        "uq_fixtures_provider_fixture_id", "fixtures", ["provider", "provider_fixture_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_fixtures_provider_fixture_id", "fixtures", type_="unique")
    op.create_unique_constraint(
        "uq_fixtures_provider_fixture_id", "fixtures", ["provider_fixture_id"]
    )

    op.drop_constraint("uq_teams_provider_name", "teams", type_="unique")
    op.create_unique_constraint("uq_teams_name", "teams", ["name"])

    op.drop_constraint("uq_leagues_provider_provider_id", "leagues", type_="unique")
    op.create_unique_constraint("uq_leagues_provider_id", "leagues", ["provider_id"])

    for table in ("fixtures", "teams", "leagues"):
        op.drop_column(table, "provider")

    op.drop_column("model_runs", "data_source")
    # odds_snapshots.data_source: DROP COLUMN — це DDL, тригер append-only
    # його не перехоплює (він row-level), тому downgrade проходить.
    op.drop_column("odds_snapshots", "data_source")
