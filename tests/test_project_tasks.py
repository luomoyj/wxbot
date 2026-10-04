from __future__ import annotations

import tempfile
import threading
import unittest
import json
from pathlib import Path

from wxbot.project.tasks import TaskStateError, TaskStore, TaskWorker, safe_task_error


class ProjectTaskTests(unittest.TestCase):
    def test_store_persists_tasks_and_recovers_interrupted_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            store = TaskStore(path)
            store.create(task_id="ABC123", project="wxbot", request="修改提示语")
            store.update("ABC123", status="running")

            recovered = TaskStore(path).recover()
            task = TaskStore(path).get("ABC123")

            self.assertEqual(len(recovered), 1)
            self.assertEqual(task.status, "failed")  # type: ignore[union-attr]
            self.assertTrue(task.notification_pending)  # type: ignore[union-attr]
            self.assertNotIn("修改提示语", task.error)  # type: ignore[union-attr]

    def test_worker_executes_queued_task_and_persists_completed_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            completed = threading.Event()
            worker = TaskWorker(
                store=store,
                executor=lambda task, _cancel: f"修改完成｜{task.id}",
                completed=lambda _task: completed.set(),
            )
            worker.start()
            worker.enqueue(task_id="ABC123", project="wxbot", request="修改提示语")

            self.assertTrue(completed.wait(2))
            task = store.get("ABC123")
            self.assertEqual(task.status, "completed")  # type: ignore[union-attr]
            self.assertEqual(task.result, "修改完成｜ABC123")  # type: ignore[union-attr]
            self.assertTrue(task.notification_pending)  # type: ignore[union-attr]
            worker.close()

    def test_confirmed_action_waits_and_survives_store_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            store = TaskStore(path)
            worker = TaskWorker(
                store=store,
                executor=lambda _task, _cancel: "done",
                completed=lambda _task: None,
            )
            task = worker.enqueue(
                task_id="ABC123", project="wxbot", request="安装项目依赖",
                confirmation_action="project_dependency_install",
                confirmation_owner="owner-hash",
                confirmation_summary="安装 requirements.txt 中声明的依赖",
            )

            restored = TaskStore(path).get(task.id)
            executed = threading.Event()
            restarted_worker = TaskWorker(
                store=TaskStore(path),
                executor=lambda _task, _cancel: executed.set() or "done",
                completed=lambda _task: None,
            )
            restarted_worker.start()
            self.assertFalse(executed.wait(0.1))
            restarted_worker.close()

            self.assertEqual(task.status, "waiting_approval")
            self.assertEqual(restored.status, "waiting_approval")  # type: ignore[union-attr]
            self.assertEqual(  # type: ignore[union-attr]
                restored.confirmation_action, "project_dependency_install",
            )
            self.assertEqual(  # type: ignore[union-attr]
                restored.confirmation_summary,
                "安装 requirements.txt 中声明的依赖",
            )

    def test_confirm_requires_matching_owner_and_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            worker = TaskWorker(
                store=store,
                executor=lambda _task, _cancel: "done",
                completed=lambda _task: None,
            )
            worker.enqueue(
                task_id="ABC123", project="wxbot", request="创建本地提交",
                confirmation_action="local_git_commit",
                confirmation_owner="owner-hash",
            )

            with self.assertRaisesRegex(TaskStateError, "用户或确认动作不匹配"):
                worker.confirm(
                    "ABC123", owner="other", action="local_git_commit",
                )
            with self.assertRaisesRegex(TaskStateError, "用户或确认动作不匹配"):
                worker.confirm(
                    "ABC123", owner="owner-hash",
                    action="project_dependency_install",
                )
            confirmed = worker.confirm(
                "ABC123", owner="owner-hash", action="local_git_commit",
            )

            self.assertEqual(confirmed.status, "queued")

    def test_confirmation_expires_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            worker = TaskWorker(
                store=store,
                executor=lambda _task, _cancel: "done",
                completed=lambda _task: None,
            )
            store.create(
                task_id="ABC123", project="wxbot", request="安装项目依赖",
                confirmation_action="project_dependency_install",
                confirmation_owner="owner-hash",
                confirmation_ttl=-1,
            )

            with self.assertRaisesRegex(TaskStateError, "确认已过期"):
                worker.confirm(
                    "ABC123", owner="owner-hash",
                    action="project_dependency_install",
                )

            self.assertEqual(store.get("ABC123").status, "cancelled")  # type: ignore[union-attr]

    def test_waiting_confirmation_can_be_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            worker = TaskWorker(
                store=store,
                executor=lambda _task, _cancel: "done",
                completed=lambda _task: None,
            )
            worker.enqueue(
                task_id="ABC123", project="wxbot", request="创建本地提交",
                confirmation_action="local_git_commit",
                confirmation_owner="owner-hash",
            )

            cancelled = worker.cancel("ABC123")

            self.assertEqual(cancelled.status, "cancelled")

    def test_worker_cancels_running_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            running = threading.Event()
            completed = threading.Event()

            def execute(_task, cancel: threading.Event) -> str:
                running.set()
                cancel.wait(2)
                raise RuntimeError("interrupted")

            worker = TaskWorker(
                store=store, executor=execute,
                completed=lambda _task: completed.set(),
            )
            worker.start()
            worker.enqueue(task_id="ABC123", project="wxbot", request="修改提示语")
            self.assertTrue(running.wait(1))

            worker.cancel("ABC123")

            self.assertTrue(completed.wait(2))
            task = store.get("ABC123")
            self.assertEqual(task.status, "cancelled")  # type: ignore[union-attr]
            self.assertEqual(task.error, "")  # type: ignore[union-attr]
            worker.close()

    def test_store_allows_multiple_queued_tasks_per_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            store.create(task_id="ABC123", project="wxbot", request="任务一")
            second = store.create(
                task_id="DEF456", project="wxbot", request="任务二",
            )

            self.assertEqual(second.status, "queued")
            self.assertEqual(store.queue_ahead(second.id), 1)

    def test_worker_resumes_persisted_queue_in_fifo_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            original = TaskStore(path)
            original.create(task_id="ABC123", project="wxbot", request="任务一")
            original.create(task_id="DEF456", project="wxbot", request="任务二")
            executed: list[str] = []
            completed = threading.Event()

            def execute(task, _cancel: threading.Event) -> str:
                executed.append(task.id)
                return task.id

            def on_completed(_task) -> None:
                if len(executed) == 2:
                    completed.set()

            worker = TaskWorker(
                store=TaskStore(path), executor=execute, completed=on_completed,
            )
            worker.start()

            self.assertTrue(completed.wait(2))
            worker.close()
            self.assertEqual(executed, ["ABC123", "DEF456"])
            self.assertEqual(
                [task.status for task in TaskStore(path).list()],
                ["completed", "completed"],
            )

    def test_legacy_task_defaults_to_no_acceptance_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            path.write_text(json.dumps([{
                "id": "ABC123",
                "project": "wxbot",
                "request": "修改提示语",
                "status": "completed",
                "created_at": 1,
                "updated_at": 2,
                "result": "已完成。",
            }]), encoding="utf-8")

            task = TaskStore(path).get("ABC123")

            self.assertEqual(task.acceptance_status, "not_required")  # type: ignore[union-attr]

    def test_store_finds_latest_pending_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            store.create(task_id="ABC123", project="wxbot", request="任务一")
            store.update(
                "ABC123", status="completed", acceptance_status="pending",
            )
            store.create(task_id="DEF456", project="wxbot", request="任务二")
            store.update(
                "DEF456", status="completed", acceptance_status="pending",
            )

            latest = store.latest_pending_acceptance(
                "wxbot", exclude_id="DEF456",
            )

            self.assertEqual(latest.id, "ABC123")  # type: ignore[union-attr]

    def test_worker_persists_sanitized_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks.json")
            completed = threading.Event()

            def execute(_task, _cancel: threading.Event) -> str:
                raise RuntimeError(
                    "读取 F:\\private\\project 失败，token=secret-value，用户 wxid_owner"
                )

            worker = TaskWorker(
                store=store, executor=execute,
                completed=lambda _task: completed.set(),
            )
            worker.start()
            worker.enqueue(task_id="ABC123", project="wxbot", request="执行任务")

            self.assertTrue(completed.wait(2))
            task = store.get("ABC123")
            self.assertEqual(task.status, "failed")  # type: ignore[union-attr]
            self.assertIn("RuntimeError", task.error)  # type: ignore[union-attr]
            self.assertNotIn("F:\\private", task.error)  # type: ignore[union-attr]
            self.assertNotIn("secret-value", task.error)  # type: ignore[union-attr]
            self.assertNotIn("wxid_owner", task.error)  # type: ignore[union-attr]
            worker.close()

    def test_safe_task_error_uses_exception_type_when_message_is_empty(self) -> None:
        self.assertEqual(safe_task_error(RuntimeError()), "RuntimeError")


if __name__ == "__main__":
    unittest.main()
