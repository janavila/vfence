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

def test_fence_editor_page():
    with TestClient(app) as client:
        response=client.get('/fences')
        assert response.status_code==200
        assert 'Editor de cerca' in response.text

def test_dashboard_uses_local_leaflet_assets():
    with TestClient(app) as client:
        response=client.get('/')
        assert '/static/vendor/leaflet/leaflet.js' in response.text
        assert 'unpkg.com/leaflet' not in response.text
        assert client.get('/static/vendor/leaflet/leaflet.js').status_code==200

def test_collar_after_packet():
    with TestClient(app) as client:
        database.save_packet(parse_message("V1|COL99|POS|-31|-54|ATENCAO|8|1.2|7"))
        response=client.get('/api/collars/COL99')
        assert response.status_code==200 and response.json()['last_sequence']==7

def test_fence_state_returns_persisted_map_points():
    fence={
        "version":7,
        "margin_attention_cm":500,
        "margin_critical_cm":200,
        "crc32":"A1B2C3D4",
        "points_e6":[[-31306000,-54064200],[-31306000,-54063700],[-31306400,-54063700]],
    }
    with TestClient(app) as client:
        database.save_pending_fence(fence,"2026-10-15T12:00:00+00:00")
        response=client.get('/api/fence-state')
        assert response.status_code==200
        data=response.json()
        assert data['downloaded_version']==7
        assert data['point_count']==3
        assert data['points'][0]['latitude']==-31.306
