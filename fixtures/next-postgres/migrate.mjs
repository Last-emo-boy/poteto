import pg from 'pg';

const client = new pg.Client({connectionString: process.env.DATABASE_URL, connectionTimeoutMillis: 5000});
await client.connect();
try {
  await client.query('CREATE TABLE IF NOT EXISTS poteto_notes (id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, text text NOT NULL)');
} finally {
  await client.end();
}
