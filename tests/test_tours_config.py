import os
import sys
from unittest.mock import MagicMock, patch
import pytest
from google.cloud import firestore

import tours_config

def test_calculate_tour_price_unknown_type():
    with pytest.raises(ValueError, match="Unknown tour type"):
        tours_config.calculate_tour_price("non_existent_tour", 5)

def test_calculate_tour_price_invalid_party_size():
    with pytest.raises(ValueError, match="Party size must be greater than 0"):
        tours_config.calculate_tour_price("private_town_tour", 0)
    with pytest.raises(ValueError, match="Party size must be greater than 0"):
        tours_config.calculate_tour_price("private_town_tour", -5)

@pytest.mark.parametrize("tour_type,party_size,expected_price", [
    # Private Town Tour ($250 base covers up to 5, then $50 each, max 20)
    ("private_town_tour", 1, 250.0),
    ("private_town_tour", 5, 250.0),
    ("private_town_tour", 6, 300.0),
    ("private_town_tour", 10, 500.0),
    ("private_town_tour", 20, 1000.0),
    
    # Living at the Lake ($250 base covers up to 5, then $50 each, max 20)
    ("living_at_the_lake", 3, 250.0),
    ("living_at_the_lake", 5, 250.0),
    ("living_at_the_lake", 7, 350.0),
    
    # Twilight Tour ($300 base covers up to 5, then $60 each, max 20)
    ("twilight_tour", 2, 300.0),
    ("twilight_tour", 5, 300.0),
    ("twilight_tour", 6, 360.0),
    ("twilight_tour", 10, 600.0),
    
    # Large Group Tour (no base, $25 flat per person, max 20)
    ("large_group_tour", 1, 25.0),
    ("large_group_tour", 10, 250.0),
    ("large_group_tour", 20, 500.0),
    
    # Mines, Mills, Rails and Ruins Tour ($400 base covers up to 4, then $100 each, max 15)
    ("mines_mills_tour", 1, 400.0),
    ("mines_mills_tour", 4, 400.0),
    ("mines_mills_tour", 5, 500.0),
    ("mines_mills_tour", 12, 1200.0),
    ("mines_mills_tour", 15, 1500.0),
])
def test_calculate_tour_price_valid_cases(tour_type, party_size, expected_price):
    price = tours_config.calculate_tour_price(tour_type, party_size)
    assert price == expected_price

@pytest.mark.parametrize("tour_type,party_size", [
    ("private_town_tour", 21),
    ("living_at_the_lake", 21),
    ("twilight_tour", 21),
    ("large_group_tour", 21),
    ("mines_mills_tour", 16),
])
def test_calculate_tour_price_exceeds_max_capacity(tour_type, party_size):
    with pytest.raises(ValueError, match="Maximum group size for"):
        tours_config.calculate_tour_price(tour_type, party_size)

def test_load_tours_config_default_no_client():
    # Calling without args falls back to local configuration
    res = tours_config.load_tours_config()
    assert res == tours_config.TOURS

def test_load_tours_config_dummy_client():
    # Verify that load_tours_config handles dummy clients properly
    mock_client = MagicMock()
    mock_client.__class__.__name__ = "DummyFirestore"
    res = tours_config.load_tours_config(mock_client)
    assert res == tours_config.TOURS

def test_load_tours_config_success():
    # Mock firestore client that successfully retrieves custom configuration document
    mock_client = MagicMock()
    mock_client.__class__.__name__ = "FirestoreClient"
    
    mock_doc = MagicMock()
    mock_doc.exists = True
    custom_tours = {"custom_tour": {"name": "Custom", "base_price": 100.0}}
    mock_doc.to_dict.return_value = {"tours": custom_tours}
    
    mock_client.collection.return_value.document.return_value.get.return_value = mock_doc
    
    res = tours_config.load_tours_config(mock_client)
    assert res == custom_tours

def test_load_tours_config_doc_does_not_exist():
    mock_client = MagicMock()
    mock_client.__class__.__name__ = "FirestoreClient"
    
    mock_doc = MagicMock()
    mock_doc.exists = False
    mock_client.collection.return_value.document.return_value.get.return_value = mock_doc
    
    res = tours_config.load_tours_config(mock_client)
    assert res == tours_config.TOURS

def test_load_tours_config_exception_fallback():
    mock_client = MagicMock()
    mock_client.__class__.__name__ = "FirestoreClient"
    mock_client.collection.side_effect = Exception("Firestore error")
    
    res = tours_config.load_tours_config(mock_client)
    assert res == tours_config.TOURS

@patch("tours_config.firestore.Client")
def test_seed_tours_success(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client
    mock_doc_ref = MagicMock()
    mock_client.collection.return_value.document.return_value = mock_doc_ref
    
    tours_config.seed_tours()
    
    mock_client_class.assert_called_once_with(database="bodie-tours")
    mock_client.collection.assert_called_once_with("config")
    mock_client.collection.return_value.document.assert_called_once_with("tours")
    mock_doc_ref.set.assert_called_once_with({"tours": tours_config.TOURS})

@patch("tours_config.firestore.Client")
def test_seed_tours_failure(mock_client_class):
    mock_client_class.side_effect = Exception("Connection refused")
    
    with pytest.raises(SystemExit) as excinfo:
        tours_config.seed_tours()
    
    assert excinfo.value.code == 1
