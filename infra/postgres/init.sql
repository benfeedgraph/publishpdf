-- Dev-only roles for docker-compose. Mirrors scripts/dev-db.sh.
CREATE ROLE publishpdf_owner LOGIN PASSWORD 'owner_dev_pw' CREATEDB;
CREATE ROLE publishpdf_app LOGIN PASSWORD 'app_dev_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE DATABASE publishpdf OWNER publishpdf_owner;
CREATE DATABASE publishpdf_test OWNER publishpdf_owner;
