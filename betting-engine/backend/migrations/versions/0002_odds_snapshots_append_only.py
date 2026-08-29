"""odds_snapshots append-only enforcement

ТЗ §7.2: odds_snapshots — append-only, кожна зміна = INSERT.
ТЗ §2:   не перезаписувати історичні snapshots.

Робимо це гарантією на рівні БД, а не домовленістю в коді: будь-який
UPDATE/DELETE (з застосунку, з psql, з міграції) впаде з помилкою.

Межа дії: row-level тригер НЕ перехоплює TRUNCATE і DROP TABLE — і це
навмисно, інакше `alembic downgrade` та скидання дев-бази стали б неможливі.
Реальний ризик, який тут закривається, — випадковий UPDATE/DELETE із коду
застосунку. У проді TRUNCATE/DDL закривається правами ролі, а не тригером.

Revision ID: 0002_append_only
"""

from alembic import op

revision = "0002_append_only"
down_revision = "59b2f97d52ba"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION odds_snapshots_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'odds_snapshots is append-only (spec 7.2): % is not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_odds_snapshots_append_only
        BEFORE UPDATE OR DELETE ON odds_snapshots
        FOR EACH ROW EXECUTE FUNCTION odds_snapshots_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_odds_snapshots_append_only ON odds_snapshots;")
    op.execute("DROP FUNCTION IF EXISTS odds_snapshots_append_only();")
