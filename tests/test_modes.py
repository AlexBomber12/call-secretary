import pytest

from call_secretary.storage import StorageError, Store


def test_simulation_database_cannot_be_used_by_a_live_worker(tmp_path):
    with Store(tmp_path / "state", mode="simulation"):
        pass
    with pytest.raises(StorageError, match="different"):
        Store(tmp_path / "state", mode="live")


def test_live_database_cannot_be_used_for_fake_reports(tmp_path):
    with Store(tmp_path / "state", mode="live"):
        pass
    with pytest.raises(StorageError, match="different"):
        Store(tmp_path / "state", mode="simulation")


def test_existing_unlabelled_real_state_is_not_repurposed_as_simulation(tmp_path):
    with Store(tmp_path / "state") as store:
        store.create_call("real-ingress")
    with pytest.raises(StorageError, match="Existing"):
        Store(tmp_path / "state", mode="simulation")
