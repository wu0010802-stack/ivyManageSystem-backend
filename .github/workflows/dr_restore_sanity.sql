-- DR Restore Drill Sanity Checks

\echo '=== 1. Core table row counts ==='
SELECT 'users' AS tbl, count(*) AS n FROM users
UNION ALL SELECT 'employees', count(*) FROM employees
UNION ALL SELECT 'students', count(*) FROM students
UNION ALL SELECT 'salary_records', count(*) FROM salary_records
UNION ALL SELECT 'attendances', count(*) FROM attendances
UNION ALL SELECT 'guardians', count(*) FROM guardians
UNION ALL SELECT 'leave_records', count(*) FROM leave_records;

\echo '=== 2. Latest event timestamps (validate freshness) ==='
SELECT 'latest_attendance' AS check, MAX(created_at)::text AS value FROM attendances
UNION ALL SELECT 'latest_audit', MAX(created_at)::text FROM audit_logs;

\echo '=== 3. Alembic head ==='
SELECT 'alembic_version' AS check, string_agg(version_num, ',') AS value FROM alembic_version;

\echo '=== 4. Cross-table join smoke ==='
SELECT u.id, u.username, e.name, e.position
FROM users u JOIN employees e ON e.user_id = u.id
LIMIT 3;
