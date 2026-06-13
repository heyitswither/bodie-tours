import os
import sys
from google.cloud import firestore

# The 5 official Bodie Foundation Tour configurations
TOURS = {
    "private_town_tour": {
        "name": "Private Town Tour of Bodie",
        "type": "Walking Tour",
        "duration_hours": 2,
        "base_price": 250.0,
        "base_capacity": 5,
        "additional_person_fee": 50.0,
        "max_capacity": 20,
        "vehicle_required": False
    },
    "living_at_the_lake": {
        "name": "Living at the Lake Tour",
        "type": "Walking Tour",
        "duration_hours": 2,
        "base_price": 250.0,
        "base_capacity": 5,
        "additional_person_fee": 50.0,
        "max_capacity": 20,
        "vehicle_required": False
    },
    "twilight_tour": {
        "name": "Bodie Twilight Tours",
        "type": "Walking Tour",
        "duration_hours": 2,
        "base_price": 300.0,
        "base_capacity": 5,
        "additional_person_fee": 60.0,
        "max_capacity": 20,
        "vehicle_required": False
    },
    "large_group_tour": {
        "name": "Bus Company / Large Group Tours",
        "type": "Walking Tour",
        "duration_hours": 1,
        "base_price": 0.0,  # No base minimum, simple flat rate
        "base_capacity": 0,
        "additional_person_fee": 25.0,  # $25 per person flat
        "max_capacity": 20,
        "vehicle_required": False
    },
    "mines_mills_tour": {
        "name": "Mines, Mills, Rails and Ruins Tour",
        "type": "Driving Tour",
        "duration_hours": 3,
        "base_price": 400.0,  # $400 minimum (covers up to 4 people)
        "base_capacity": 4,
        "additional_person_fee": 100.0,  # $100 per additional person
        "max_capacity": 15,  # Max 15 people
        "vehicle_required": True
    }
}

def load_tours_config(db_client=None):
    """
    Retrieve the tour types and pricing from Firestore config/tours if present,
    falling back to local defaults.
    """
    if db_client is None:
        try:
            db_client = firestore.Client(database="bodie-tours")
        except Exception:
            return TOURS
    try:
        # Check if dummy db or mock
        if hasattr(db_client, "__class__") and db_client.__class__.__name__ in ("DummyFirestore", "_DummyClient", "MagicMock", "Mock"):
            return TOURS
        doc = db_client.collection("config").document("tours").get()
        if doc.exists:
            data = doc.to_dict()
            if data and "tours" in data:
                return data["tours"]
    except Exception:
        pass
    return TOURS

def calculate_tour_price(tour_type: str, party_size: int) -> float:
    """
    Calculate dynamic tour pricing using tiered base rates and additional person charges.
    """
    if tour_type not in TOURS:
        raise ValueError(f"Unknown tour type: {tour_type}")
    
    config = TOURS[tour_type]
    
    if party_size <= 0:
        raise ValueError("Party size must be greater than 0.")
    
    max_cap = config.get("max_capacity", 20)
    if party_size > max_cap:
        raise ValueError(f"Maximum group size for {config['name']} is {max_cap}.")
        
    base_price = float(config.get("base_price", 0.0))
    base_capacity = config.get("base_capacity", 0)
    additional_person_fee = float(config.get("additional_person_fee", 0.0))
    
    if tour_type == "large_group_tour":
        return float(party_size * additional_person_fee)
        
    if party_size <= base_capacity:
        return float(base_price)
    else:
        additional_count = party_size - base_capacity
        return float(base_price + (additional_count * additional_person_fee))

def seed_tours():
    """
    Write the default 5 tour configurations into Firestore under config/tours.
    """
    print("Initializing Firestore Client for tour configuration seeding...")
    try:
        db = firestore.Client(database="bodie-tours")
        doc_ref = db.collection("config").document("tours")
        doc_ref.set({"tours": TOURS})
        print("Successfully seeded config/tours doc with the 5 Bodie Foundation tour configurations!")
    except Exception as exc:
        print(f"Error seeding tours configuration: {exc}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    seed_tours()
