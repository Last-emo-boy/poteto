import {timingSafeEqual} from 'node:crypto';
import {database} from '../../../lib/database';

export const dynamic = 'force-dynamic';
export async function POST(request) {
  const supplied = Buffer.from(request.headers.get('x-test-key') || '');
  const expected = Buffer.from(process.env.TEST_WRITE_KEY || '');
  if (!expected.length || supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) {
    return Response.json({error: 'unauthorized'}, {status: 401});
  }
  const body = await request.text();
  if (Buffer.byteLength(body) > 1024) return Response.json({error: 'too large'}, {status: 413});
  let text;
  try { text = JSON.parse(body).text; } catch { return Response.json({error: 'invalid JSON'}, {status: 400}); }
  if (typeof text !== 'string' || !text.trim() || text.length > 200) {
    return Response.json({error: 'text must contain 1–200 characters'}, {status: 400});
  }
  const {rows} = await database().query('INSERT INTO poteto_notes (text) VALUES ($1) RETURNING id', [text]);
  return Response.json({id: rows[0].id}, {status: 201});
}
