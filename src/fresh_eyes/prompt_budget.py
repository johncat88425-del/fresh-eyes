"""Reviewed byte-BPE bound for two plain-text Nemotron chat messages.

No tokenizer code or Jinja template is executed. Reference files are data only.
Provider tokenizer/template compatibility is an explicit configuration assumption;
this client reservation is not an account-level billing limit.
"""

import hashlib
import json
from pathlib import Path

from .models import ModelError


PROFILES = {
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": {
        "role": "nano",
        "tokenizer_sha256": "c6021eb6847e682f89aa52d5eb6e8c7d902a23acfc8137e25211cf84828f1592",
        "template_sha256": "ab7813c3abdd9cb655905a410728b26c7884eca45ddfab8d9f931553485a7862",
    },
    "nvidia/Nemotron-3-Ultra-550b-a55b": {
        "role": "ultra",
        "tokenizer_sha256": "623c34567aebb18582765289fbe23d901c62704d6518d71866e0e58db892b5b7",
        "template_sha256": "82753bef5cedc4932c1ed509b5c9a12be680fd86d1adb65bc3f7398d11c8eebc",
    },
}
HEADER_TOKEN_ALLOWANCE = 2048


class NemotronPromptBound:
    def __init__(self, reference_dir):
        root = Path(reference_dir)
        self.references = {}
        for model_id, profile in PROFILES.items():
            role = profile["role"]
            path = root / (role + "-tokenizer.json")
            if path.stat().st_size > 24 * 1024 * 1024:
                raise ModelError("invalid_prompt_bound", "Tokenizer reference is too large.")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != profile["tokenizer_sha256"]:
                raise ModelError("invalid_prompt_bound", "Tokenizer reference differs from the reviewed profile.")
            data = json.loads(raw)
            pre = data.get("pre_tokenizer", {})
            stages = pre.get("pretokenizers", []) if pre.get("type") == "Sequence" else []
            if (data.get("normalizer") is not None or data.get("model", {}).get("type") != "BPE" or
                len(stages) != 2 or stages[0].get("type") != "Split" or
                stages[1].get("type") != "ByteLevel" or stages[1].get("add_prefix_space") is not False or
                data.get("post_processor", {}).get("type") != "ByteLevel"):
                raise ModelError("invalid_prompt_bound", "Only the reviewed non-expanding byte-BPE tokenizer is supported.")
            if role == "nano":
                template = json.loads((root / "nano-tokenizer_config.json").read_text())["chat_template"]
            else:
                template = (root / "ultra-chat_template.jinja").read_text()
            if hashlib.sha256(template.encode()).hexdigest() != profile["template_sha256"]:
                raise ModelError("invalid_prompt_bound", "Chat template differs from the reviewed two-message profile.")
            self.references[model_id] = ("nvidia-byte-bpe-v1:" + profile["tokenizer_sha256"] +
                                         ":template:" + profile["template_sha256"] + ":header=2048")

    def __call__(self, spec, messages):
        if spec.model_id not in self.references or [m.get("role") for m in messages] != ["system", "user"] or any(
            set(m) != {"role", "content"} or not isinstance(m["content"], str) for m in messages
        ):
            raise ModelError("invalid_prompt_bound", "The reviewed bound requires exactly one system and one user text message.")
        # ByteLevel maps each UTF-8 byte to one symbol; Split inserts no symbols;
        # BPE merges only reduce that count. The reviewed plain-text template
        # inserts <128 bytes. Reserve 2048 additional tokens for framing instead.
        tokens = sum(len(m["content"].encode("utf-8")) for m in messages) + HEADER_TOKEN_ALLOWANCE
        if tokens + spec.max_completion_tokens > spec.context_window:
            raise ModelError("context_too_large", "The bounded request exceeds the verified model context.")
        return tokens, self.references[spec.model_id]
