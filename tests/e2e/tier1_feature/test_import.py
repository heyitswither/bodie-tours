import traceback
def test_import():
    try:
        import main
    except Exception as e:
        traceback.print_exc()
        assert False, f"Import failed: {e}"
