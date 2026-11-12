import pg from 'pg';

let pool;
export function database() {
  if (!process.env.DATABASE_URL) throw new Error('DATABASE_URL is required at runtime');
  pool ??= new pg.Pool({connectionString: process.env.DATABASE_URL, max: 2, connectionTimeoutMillis: 5000});
  return pool;
}
