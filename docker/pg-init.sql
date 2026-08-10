-- Runs once when the hdh-pgdata volume is first created.
-- hdh      : the development database (HDH_DB_URL)
-- hdh_test : scratch database the PostgreSQL integration tests may freely
--            create and drop tables in (HDH_PG_TEST_URL)
CREATE DATABASE hdh_test OWNER hdh;
