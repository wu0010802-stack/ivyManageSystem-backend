"""services/task_registry.py 的單元測試。"""

import time

import pytest

from services.task_registry import TaskRegistry, TaskStatus


@pytest.fixture
def registry():
    return TaskRegistry()


class TestTaskRegistry:
    def test_create_returns_queued_task_with_unique_id(self, registry):
        a = registry.create("export_employees")
        b = registry.create("export_employees")
        assert a.task_id != b.task_id
        assert a.status == TaskStatus.QUEUED
        assert a.progress == 0.0
        assert a.kind == "export_employees"

    def test_update_transitions_status(self, registry):
        task = registry.create("scrape")
        registry.update(task.task_id, status=TaskStatus.RUNNING, progress=0.3)
        updated = registry.get(task.task_id)
        assert updated.status == TaskStatus.RUNNING
        assert updated.progress == 0.3

    def test_progress_clamped_to_0_1(self, registry):
        task = registry.create("x")
        registry.update(task.task_id, progress=1.5)
        assert registry.get(task.task_id).progress == 1.0
        registry.update(task.task_id, progress=-0.2)
        assert registry.get(task.task_id).progress == 0.0

    def test_update_unknown_id_returns_none(self, registry):
        assert registry.update("not-a-real-id", status=TaskStatus.RUNNING) is None

    def test_completed_task_preserves_result(self, registry):
        task = registry.create("export_salary")
        registry.update(
            task.task_id,
            status=TaskStatus.COMPLETED,
            progress=1.0,
            result={"file_path": "/tmp/salary.xlsx"},
        )
        final = registry.get(task.task_id)
        assert final.status == TaskStatus.COMPLETED
        assert final.result == {"file_path": "/tmp/salary.xlsx"}

    def test_failed_task_records_error(self, registry):
        task = registry.create("import_students")
        registry.update(
            task.task_id,
            status=TaskStatus.FAILED,
            error="找不到學生 ID 404",
        )
        failed = registry.get(task.task_id)
        assert failed.status == TaskStatus.FAILED
        assert failed.error == "找不到學生 ID 404"

    def test_list_filters_by_kind(self, registry):
        registry.create("a")
        registry.create("a")
        registry.create("b")
        assert len(registry.list("a")) == 2
        assert len(registry.list("b")) == 1
        assert len(registry.list()) == 3

    def test_to_dict_serializes_enum(self, registry):
        task = registry.create("x")
        registry.update(task.task_id, status=TaskStatus.RUNNING)
        data = registry.to_dict(registry.get(task.task_id))
        assert data["status"] == "running"
        assert data["task_id"] == task.task_id

    def test_prune_removes_old_records(self):
        reg = TaskRegistry(retention_seconds=60)
        task = reg.create("x")
        # 手動把 updated_at 推到過去一小時前，確保 prune 判斷可靠
        record = reg.get(task.task_id)
        record.updated_at = time.time() - 3600
        removed = reg.prune()
        assert removed == 1
        assert reg.get(task.task_id) is None

    def test_prune_preserves_fresh_records(self, registry):
        task = registry.create("x")
        assert registry.prune() == 0
        assert registry.get(task.task_id) is not None
