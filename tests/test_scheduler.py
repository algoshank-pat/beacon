from app.scheduler import SchedulerAlreadyRunningError, acquire_single_instance_lock


def test_acquire_single_instance_lock_succeeds_when_uncontended(tmp_path):
    lock_path = tmp_path / "scheduler.lock"
    lock_file = acquire_single_instance_lock(lock_path)
    try:
        assert lock_path.exists()
    finally:
        lock_file.close()


def test_acquire_single_instance_lock_rejects_a_second_holder(tmp_path):
    lock_path = tmp_path / "scheduler.lock"
    first = acquire_single_instance_lock(lock_path)
    try:
        try:
            acquire_single_instance_lock(lock_path)
            assert False, "expected SchedulerAlreadyRunningError"
        except SchedulerAlreadyRunningError:
            pass
    finally:
        first.close()


def test_acquire_single_instance_lock_available_again_after_release(tmp_path):
    lock_path = tmp_path / "scheduler.lock"
    first = acquire_single_instance_lock(lock_path)
    first.close()  # simulates the holder exiting (including an ungraceful kill)

    second = acquire_single_instance_lock(lock_path)
    try:
        assert lock_path.exists()
    finally:
        second.close()
