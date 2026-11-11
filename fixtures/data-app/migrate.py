from database import query

query('BEGIN; CREATE TABLE IF NOT EXISTS records(id bigserial PRIMARY KEY,value text NOT NULL); COMMIT;')
print('Migration completed')
