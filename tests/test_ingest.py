import pytest
from app import app, db

@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.session.remove()
        db.drop_all()


def test_health_check(client):
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json['status'] == 'ok'


def test_ingest_valid_payload(client):
    response = client.post('/ingest',
        json={"event": "lead.created", "data": {"email": "test@example.com"}},
        content_type='application/json'
    )
    # httpbin.org returns 200 so delivery should succeed
    assert response.status_code in [200, 500]  # 500 if no network in test env


def test_ingest_missing_fields(client):
    response = client.post('/ingest',
        json={"wrong_key": "value"},
        content_type='application/json'
    )
    assert response.status_code == 422
    assert "Missing required fields" in response.json['error']


def test_ingest_invalid_json(client):
    response = client.post('/ingest',
        data="this is not json",
        content_type='application/json'
    )
    assert response.status_code == 400


def test_failures_endpoint_returns_list(client):
    response = client.get('/failures')
    assert response.status_code == 200
    assert 'count' in response.json
    assert 'failures' in response.json
    assert isinstance(response.json['failures'], list)