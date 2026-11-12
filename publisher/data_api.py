from fastapi import Depends
from publisher.data_store import DataStore
from publisher.models import DataInput
from publisher.store import PREVIEW_SCOPE


def register(app, store, actor):
    data = DataStore(store)

    @app.post('/v1/projects/{project_id}/databases',status_code=202,tags=['Data'])
    def create_database(project_id: str, body: DataInput, user=Depends(actor)):
        return data.create(user,project_id,'database',body.stage)

    @app.post('/v1/projects/{project_id}/volumes',status_code=202,tags=['Data'])
    def create_volume(project_id: str, body: DataInput, user=Depends(actor)):
        return data.create(user,project_id,'volume',body.stage)

    @app.get('/v1/projects/{project_id}/data',tags=['Data'])
    def list_data(project_id: str, user=Depends(actor)):
        with store.connection() as db:
            data.project(db,user,project_id,'projects:read')
            rows=db.execute('SELECT * FROM data_resources WHERE project_id=? ORDER BY created',(project_id,)).fetchall()
            return [data.view(r) for r in rows if PREVIEW_SCOPE not in user['scopes'] or r['stage']=='preview']

    @app.get('/v1/data/{resource_id}',tags=['Data'])
    def get_data(resource_id: str, user=Depends(actor)):
        with store.connection() as db:
            return data.view(data.resource(db,user,resource_id,scope='projects:read'))

    @app.post('/v1/data/{resource_id}/retry',status_code=202,tags=['Data'])
    def retry(resource_id: str, user=Depends(actor)):
        return data.queue(user,resource_id,'retry')

    @app.delete('/v1/data/{resource_id}',status_code=202,tags=['Data'])
    def delete(resource_id: str, destroy_data: bool=False, user=Depends(actor)):
        return data.queue(user,resource_id,'delete',destroy_data)
