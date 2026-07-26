import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from comiset.llama_cpp_client import LlamaCppGateway, parse_model_ref


class ParseModelRefTests(unittest.TestCase):
    def test_parses_hugging_face_gguf_reference(self) -> None:
        ref = parse_model_ref("org/model:Q4_K_M.gguf")
        self.assertEqual((ref.repo_id, ref.filename), ("org/model", "Q4_K_M.gguf"))

    def test_parses_revision(self) -> None:
        ref = parse_model_ref("org/model@abc123:Q4_K_M.gguf")
        self.assertEqual((ref.repo_id, ref.revision, ref.filename), ("org/model", "abc123", "Q4_K_M.gguf"))

    def test_rejects_non_gguf_reference(self) -> None:
        with self.assertRaises(ValueError):
            parse_model_ref("llama3.2:3b")


class GatewayPreparationTests(unittest.TestCase):
    def test_separates_prepare_warmup_and_inference(self) -> None:
        calls = []

        class FakeLlama:
            def __init__(self, **kwargs):
                self.metadata = {"general.file_type": 15}
                calls.append(("load", kwargs))

            def create_chat_completion(self, **kwargs):
                calls.append(("chat", kwargs))
                return {"choices": [{"message": {"content": "OK"}}]}

        llama_module = types.ModuleType("llama_cpp")
        llama_module.Llama = FakeLlama
        llama_module.llama_print_system_info = lambda: b"CPU test backend"
        hub_module = types.ModuleType("huggingface_hub")

        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "snapshots" / "resolved-sha" / "model.gguf"
            model_path.parent.mkdir(parents=True)
            model_path.write_bytes(b"fake gguf")
            hub_module.hf_hub_download = lambda **kwargs: str(model_path)
            with patch.dict(sys.modules, {"llama_cpp": llama_module, "huggingface_hub": hub_module}):
                gateway = LlamaCppGateway(4096, -1, 512, seed=7, max_output_tokens=32)
                manifest = gateway.prepare("org/model@main:model.gguf", warmup_runs=1)
                gateway.chat("org/model@main:model.gguf", "system", "user")

        self.assertEqual([name for name, _ in calls], ["load", "chat", "chat"])
        self.assertEqual(manifest["resolved_revision"], "resolved-sha")
        self.assertEqual(manifest["setup_timing"]["warmup_runs"], 1)
        self.assertEqual(manifest["configuration"]["seed"], 7)
        self.assertEqual(calls[-1][1]["max_tokens"], 32)


if __name__ == "__main__":
    unittest.main()
