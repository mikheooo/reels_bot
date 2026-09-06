from sqlalchemy.dialects import postgresql

from app.bot.handlers import existing_job_query


def test_existing_job_query_does_not_filter_out_error_status():
    sql = str(
        existing_job_query("hash").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "jobs.url_hash = 'hash'" in sql
    where_clause = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "jobs.status" not in where_clause
    assert "jobs.created_at DESC" in sql
