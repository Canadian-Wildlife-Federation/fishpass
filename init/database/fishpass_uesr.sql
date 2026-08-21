CREATE ROLE <role> WITH LOGIN PASSWORD '<password>';
GRANT CONNECT ON DATABASE chyf TO fishpass;

-- in chyf database
GRANT USAGE ON SCHEMA chyf2 TO fishpass;
GRANT SELECT ON ALL TABLES IN SCHEMA chyf2 TO fishpass;
ALTER DEFAULT PRIVILEGES IN SCHEMA chyf2 GRANT SELECT ON TABLES TO fishpass;


-- in fishpass_dev database
ALTER DATABASE fishpass_dev OWNER TO fishpass;
ALTER SCHEMA public OWNER TO fishpass;

GRANT USAGE ON FOREIGN DATA WRAPPER postgres_fdw TO fishpass;