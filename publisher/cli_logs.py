"""Portable runtime log polling with bounded, resumable cursors."""
import http.client
import json
import ssl
import sys
import time
import urllib.error
from urllib.parse import urlencode

if __package__:
    from .cli_upload import RequestError
else:
    from cli_upload import RequestError


def terminal_text(message):
    return ''.join(char if char in '\n\t' or (ord(char) >= 32 and not 127 <= ord(char) <= 159
                   and char not in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')
                   else repr(char)[1:-1] for char in message)


def runtime(client, deployment, *, follow=False, timeout=900, tail=200, after=None, jsonl=False):
    deadline = time.monotonic() + (timeout if follow else min(timeout, 45))
    cursor, interrupted, previous_state = after, False, None

    def emit(entry):
        if jsonl:
            print(json.dumps({'deployment_id': deployment, **entry}, ensure_ascii=False), flush=True)
        elif entry['kind'] == 'gap':
            print(entry['message'], file=sys.stderr, flush=True)
        else:
            print(terminal_text(entry['message']), end='' if entry['message'].endswith('\n') else '\n', flush=True)

    while (remaining := deadline - time.monotonic()) > 0:
        query = {'tail': tail}
        if cursor is not None:
            query['after'] = cursor
        path = '/v1/deployments/' + deployment + '/runtime-logs?' + urlencode(query)
        try:
            result = client.request('GET', path, timeout=min(30, remaining))
        except (OSError, http.client.HTTPException, RequestError) as error:
            cause = error.reason if isinstance(error, urllib.error.URLError) else error
            if isinstance(cause, ssl.SSLCertVerificationError):
                raise
            if not follow or isinstance(error, RequestError) and error.status not in {429, 500, 502, 503, 504}:
                raise
            if not interrupted:
                print('Connection interrupted; resuming runtime logs from the last cursor', file=sys.stderr, flush=True)
                interrupted = True
            time.sleep(min(60 if isinstance(error, RequestError) and error.status == 429 else 2, max(0, deadline-time.monotonic())))
            continue
        interrupted = False
        if follow or jsonl or after is not None or tail != 200:
            if not {'entries', 'next_cursor', 'truncated'} <= result.keys():
                raise RuntimeError('This server does not support runtime log cursors; update the server or omit --follow, --jsonl, --after and --tail')
        if result['error']:
            raise RuntimeError(result['error'])
        if not follow:
            if result['state'] not in {'queued', 'running'}:
                if jsonl:
                    if result['truncated']:
                        emit({'kind':'gap', 'message':'Some runtime logs are no longer retained; continuing with available entries.'})
                    for entry in result['entries']:
                        emit(entry)
                    return None
                return result
        else:
            if result['truncated']:
                emit({'kind':'gap', 'message':'Some runtime logs are no longer retained; continuing with available entries.'})
            for entry in result['entries']:
                emit(entry)
            # A cold collector has not established the initial tail yet. Keep
            # tail=0 (or a limited tail) until the first snapshot exists.
            if cursor is not None or result.get('collected') is not None or result['state']=='completed' or result['next_cursor']>0:
                cursor = result['next_cursor']
            if result['state'] == 'paused' and previous_state != 'paused':
                print('Runtime log collection is paused; waiting without waking the application', file=sys.stderr, flush=True)
            previous_state = result['state']
        delay = 1 if result['state'] in {'queued', 'running'} else 5
        time.sleep(min(delay, max(0, deadline-time.monotonic())))
    if not follow:
        raise RuntimeError('Runtime logs are still being collected; repeat this command to observe the same operation')
    return None
