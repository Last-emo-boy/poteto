"""Bounded, persistent cursors over redacted runtime snapshots."""
from publisher.store import Problem

LIMIT = 65536
LINES = 200


def save(db, deployment_id, content, collected):
    previous = db.execute('SELECT content FROM runtime_logs WHERE deployment_id=?', (deployment_id,)).fetchone()
    head = db.execute('SELECT cursor FROM runtime_log_heads WHERE deployment_id=?', (deployment_id,)).fetchone()
    cursor = head['cursor'] if head else 0
    old = previous['content'].splitlines(keepends=True) if previous and head else []
    lines = content.splitlines(keepends=True)
    overlap = min(len(old), len(lines))
    while overlap and old[-overlap:] != lines[:overlap]:
        overlap -= 1
    additions = [('line', line) for line in lines[overlap:]]
    if old and not overlap and old != lines:
        additions.insert(0, ('gap', 'Runtime log continuity unavailable; the container log may have rotated or exceeded the capture window.'))
    for kind, message in additions:
        cursor += 1
        db.execute('INSERT INTO runtime_log_entries VALUES(?,?,?,?,?)', (deployment_id, cursor, kind, message, collected))
    db.execute('INSERT INTO runtime_log_heads VALUES(?,?) ON CONFLICT(deployment_id) DO UPDATE SET cursor=excluded.cursor', (deployment_id, cursor))
    # The cursor survives eviction and process restarts. The journal does not grow
    # with application uptime, including applications which print giant lines.
    size, count, cutoff = 0, 0, None
    for row in db.execute('SELECT cursor,message FROM runtime_log_entries WHERE deployment_id=? ORDER BY cursor DESC', (deployment_id,)):
        size += len(row['message'].encode())
        count += 1
        if size > LIMIT or count > LINES:
            cutoff = row['cursor']
            break
    if cutoff is not None:
        db.execute('DELETE FROM runtime_log_entries WHERE deployment_id=? AND cursor<=?', (deployment_id, cutoff))
    db.execute('INSERT INTO runtime_logs VALUES(?,?,?) ON CONFLICT(deployment_id) DO UPDATE SET content=excluded.content,collected=excluded.collected', (deployment_id, content, collected))


def read(db, deployment_id, after, tail):
    head = db.execute('SELECT cursor FROM runtime_log_heads WHERE deployment_id=?', (deployment_id,)).fetchone()
    cursor = head['cursor'] if head else 0
    if after is not None and after > cursor:
        raise Problem(409, 'Runtime log cursor is ahead of this deployment; omit --after to start again')
    rows = db.execute('SELECT cursor,kind,message,collected FROM runtime_log_entries WHERE deployment_id=? AND cursor>? ORDER BY cursor DESC LIMIT ?',
                      (deployment_id, after if after is not None else -1, LINES if after is not None else tail)).fetchall()
    entries = [dict(row) for row in reversed(rows)]
    first = entries[0]['cursor'] if entries else cursor + 1
    truncated = after is not None and after < first - 1
    return {'entries': entries, 'next_cursor': cursor, 'truncated': truncated}
