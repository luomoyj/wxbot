import json
import tempfile
import unittest
from pathlib import Path

from wxbot.ai.model_config import ModelConfig
from wxbot.ai.turn_metrics import TurnMetricsStore


class ModelConfigAndMetricsTests(unittest.TestCase):
    def test_model_config_accepts_only_non_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            path.write_text(
                json.dumps({"model": "gpt-5.6-sol", "reasoning_effort": "low"}),
                encoding="utf-8",
            )
            config = ModelConfig.load(path)
            self.assertEqual(config.model, "gpt-5.6-sol")
            self.assertEqual(config.reasoning_effort, "low")
            path.write_text(
                json.dumps({"model": "gpt-5.6-sol", "reasoning_effort": "low", "token": "x"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "只允许"):
                ModelConfig.load(path)

    def test_metrics_store_does_not_persist_message_or_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.json"
            store = TurnMetricsStore(path)
            store.record(
                request_type="project_query", stage="answer", input_length=88,
                model="gpt-5.6-sol", reasoning_effort="low",
                result="success", model_seconds=1.23456, total_seconds=1.34567,
            )
            text = path.read_text(encoding="utf-8")
            payload = json.loads(text)
            record = payload["records"][0]
            self.assertEqual(record["model_seconds"], 1.235)
            self.assertEqual(record["total_seconds"], 1.346)
            self.assertEqual(record["request_type"], "project_query")
            self.assertEqual(record["stage"], "answer")
            self.assertEqual(record["input_size_bucket"], "0-100")
            self.assertRegex(record["request_id"], r"^[0-9a-f]{12}$")
            self.assertNotIn("message", text)
            self.assertNotIn("thread", text.lower())

    def test_input_size_buckets_are_bounded(self) -> None:
        self.assertEqual(TurnMetricsStore._input_size_bucket(100), "0-100")
        self.assertEqual(TurnMetricsStore._input_size_bucket(101), "101-500")
        self.assertEqual(TurnMetricsStore._input_size_bucket(501), "501-2000")
        self.assertEqual(TurnMetricsStore._input_size_bucket(2001), "2001+")


if __name__ == "__main__":
    unittest.main()
