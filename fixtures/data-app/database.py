import json
import os
import subprocess
from urllib.parse import urlsplit,unquote


def query(sql):
    url=urlsplit(os.environ['DATABASE_URL'])
    environment=os.environ|{'PGHOST':url.hostname,'PGPORT':str(url.port or 5432),'PGUSER':unquote(url.username),
        'PGPASSWORD':unquote(url.password),'PGDATABASE':url.path.lstrip('/'),'PGCONNECT_TIMEOUT':'3',
        'PGOPTIONS':'-c standard_conforming_strings=on'}
    result=subprocess.run(['psql','-X','-qAt','-v','ON_ERROR_STOP=1'],input=sql,text=True,capture_output=True,env=environment,timeout=8)
    if result.returncode:
        raise RuntimeError('Database query failed')
    return result.stdout.strip()


def literal(value):
    if not isinstance(value,str) or len(value)>2000 or '\x00' in value:
        raise ValueError('Value must be text, at most 2000 characters')
    return "'"+value.replace("'","''")+"'"
