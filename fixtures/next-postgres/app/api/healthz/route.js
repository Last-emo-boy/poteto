import {database} from '../../../lib/database';

export const dynamic = 'force-dynamic';
export async function GET() {
  const {rows} = await database().query('SELECT count(*)::integer AS count FROM poteto_notes');
  return Response.json({status: 'ok', version: process.env.APP_VERSION, count: rows[0].count});
}
