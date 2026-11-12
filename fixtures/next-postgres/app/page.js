import {database} from '../lib/database';

export const dynamic = 'force-dynamic';
export default async function Page() {
  const {rows} = await database().query('SELECT count(*)::integer AS count FROM poteto_notes');
  return <main>
    <h1>SSR + PostgreSQL</h1>
    <p>Image: <strong>{process.env.APP_VERSION}</strong></p>
    <p>Saved notes: <strong>{rows[0].count}</strong></p>
  </main>;
}
