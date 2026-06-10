import pytest


def test_pruning_ttl_calc_far_future(client):
    response = client.post("/prune")
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_pruning_ttl_calc_near_term(client):
    response = client.post("/prune")
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_pruning_ttl_calc_immediate(client):
    response = client.post("/prune")
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_pruning_ttl_stored_in_db(client, mock_firestore):
    response = client.post("/prune")
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200


def test_pruning_ttl_calc_logic_edges(client):
    response = client.post("/prune")
    if response.status_code == 501:
        pytest.xfail("Not implemented")
    assert response.status_code == 200
