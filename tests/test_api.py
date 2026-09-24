import os
os.environ["DATABASE_URL"]="sqlite:///:memory:"
os.environ["VFENCE_MOCK_SERIAL"]="false"
from fastapi.testclient import TestClient
from app.main import app,database
from app.protocol import parse_message

def test_health_and_empty_collars():
    with TestClient(app) as client:
        assert client.get('/api/health').json()['status']=='ok'
        assert isinstance(client.get('/api/collars').json(),list)

def test_collar_after_packet():
    with TestClient(app) as client:
        database.save_packet(parse_message("V1|COL99|POS|-31|-54|ATENCAO|8|1.2|7"))
        response=client.get('/api/collars/COL99')
        assert response.status_code==200 and response.json()['last_sequence']==7
