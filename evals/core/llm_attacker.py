"""攻擊者 (Attacker):負責生成對抗測試 case。

兩種實作:
- HeuristicAttacker: 不需 API key。從 signature 推導邊界值 + 隨機 mutation。
- LLMAttacker: 透過 Anthropic Claude API 生成。需 ANTHROPIC_API_KEY。

選用優先序由 build_attacker(mode) 控制:
- "auto": 有 ANTHROPIC_API_KEY → LLM,否則 → heuristic
- "llm" / "heuristic": 強制
"""

from __future__ import annotations

import json
import logging
import os
import random
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class Attacker(ABC):
    name: str = "abstract"

    @abstractmethod
    def generate(
        self, target, n: int, prior_findings: list[dict] | None = None
    ) -> list[dict]:
        """回傳 n 個 case input dict。"""


class HeuristicAttacker(Attacker):
    """規則式攻擊器:從 schema 推導邊界值與惡意 mutation。"""

    name = "heuristic"

    NUMERIC_BOUNDARY = [
        0,
        1,
        -1,
        2,
        -2,
        100,
        -100,
        10_000,
        -10_000,
        2**31 - 1,
        -(2**31),
        2**63 - 1,
    ]
    FLOAT_BOUNDARY = [
        0.0,
        -0.0,
        0.5,
        -0.5,
        1e-9,
        -1e-9,
        1e9,
        -1e9,
        float("inf"),
        float("-inf"),
    ]
    STRING_BOUNDARY = [
        "",
        " ",
        "x" * 1000,
        "../../etc/passwd",
        "<script>alert(1)</script>",
        "'; DROP TABLE x; --",
        "\x00",
        "你好",
        "🎉",
    ]

    def __init__(self, seed: int = 1337) -> None:
        self.rng = random.Random(seed)

    def generate(
        self, target, n: int, prior_findings: list[dict] | None = None
    ) -> list[dict]:
        cases: list[dict] = []
        # 1) seed cases 做 single-field perturbation
        for seed_case in target.seed_cases:
            for field_name, spec in target.signature.get("fields", {}).items():
                for val in self._boundary_values(spec):
                    mutated = dict(seed_case)
                    mutated[field_name] = val
                    cases.append(mutated)
                    if len(cases) >= n * 3:  # 多生點供之後 sampling
                        break
                if len(cases) >= n * 3:
                    break
            if len(cases) >= n * 3:
                break

        # 2) all-fields random sample from boundary
        for _ in range(max(n - len(cases), n // 2)):
            cases.append(self._random_case(target))

        # 3) de-dupe by JSON repr, cap to n
        seen: set[str] = set()
        unique: list[dict] = []
        for c in cases:
            try:
                key = json.dumps(c, default=str, sort_keys=True)
            except Exception:
                key = repr(c)
            if key in seen:
                continue
            seen.add(key)
            unique.append(c)
        self.rng.shuffle(unique)
        return unique[:n]

    def _boundary_values(self, spec: dict) -> list[Any]:
        type_ = spec.get("type", "any")
        if type_ == "int":
            vals = list(self.NUMERIC_BOUNDARY)
            for b in spec.get("boundary", []):
                if b is None:
                    vals.append(None)
                    continue
                if not isinstance(b, (int, float)) or isinstance(b, bool):
                    vals.append(b)  # 非數值 boundary(如 string "NaN")原樣放入
                    continue
                vals.extend([b - 1, b, b + 1])
            return vals
        if type_ == "float":
            vals = list(self.FLOAT_BOUNDARY)
            for b in spec.get("boundary", []):
                if b is None:
                    vals.append(None)
                    continue
                if not isinstance(b, (int, float)) or isinstance(b, bool):
                    vals.append(b)
                    continue
                vals.extend([b - 1e-6, b, b + 1e-6])
            return vals
        if type_ == "string":
            base = list(self.STRING_BOUNDARY)
            if spec.get("enum"):
                base.extend(spec["enum"])
                base.append("__not_in_enum__")
            return base
        if type_ == "bool":
            return [True, False]
        if type_ == "date":
            today = date.today()
            return [
                today,
                today - timedelta(days=1),
                today + timedelta(days=1),
                today - timedelta(days=365),
                today + timedelta(days=365),
                date(1900, 1, 1),
                date(9999, 12, 31),
            ]
        if type_ == "dict":
            return [None, {}, {"unexpected_key": "x"}]
        return [None]

    def _random_case(self, target) -> dict:
        case: dict = {}
        for name, spec in target.signature.get("fields", {}).items():
            vals = self._boundary_values(spec)
            case[name] = self.rng.choice(vals)
        return case


class LLMAttacker(Attacker):
    """用 Anthropic Claude 生成攻擊 case。

    Prompt 帶: target signature、invariants 描述、seed_cases、prior_findings。
    要求 Claude 回 JSON array,每個 element 是 input dict + 「為何認為會打破不變量」。
    """

    name = "llm"

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        api_key: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "anthropic SDK 未安裝。pip install anthropic 或改用 heuristic mode。"
            ) from exc
        self.client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )
        self.model = model
        self.max_tokens = max_tokens

    def generate(
        self, target, n: int, prior_findings: list[dict] | None = None
    ) -> list[dict]:
        prompt = self._build_prompt(target, n, prior_findings or [])
        logger.info("LLMAttacker requesting %d cases for %s", n, target.name)
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in msg.content if hasattr(block, "text"))
        return self._parse(text, n)

    def _build_prompt(self, target, n: int, prior_findings: list[dict]) -> str:
        inv_lines = "\n".join(
            f"- {inv.name}: {inv.description}" for inv in target.invariants
        )
        seed_block = json.dumps(
            target.seed_cases[:3], default=str, ensure_ascii=False, indent=2
        )
        prior_block = (
            json.dumps(prior_findings[:5], default=str, ensure_ascii=False, indent=2)
            if prior_findings
            else "(none yet)"
        )
        return f"""你是對抗測試生成器,目標是打破目標函式的不變量,找出 edge case 與潛在 bug。

# 目標 (Target)
名稱: {target.name}
說明: {target.description}

# 介面 (Signature)
{json.dumps(target.signature, ensure_ascii=False, indent=2)}

# 不變量 (Invariants) — 你的攻擊就是要讓它們失敗
{inv_lines}

# Seed 範例(已知合法 case,只是給你看介面)
{seed_block}

# 之前的發現(已找到的違反)
{prior_block}

# 任務
生成 {n} 個對抗 case。每個 case 應該:
1. 試圖讓至少一個不變量失敗(明示推測哪一個)
2. 探索邊界、極值、捨入、NaN、空集、組合突變
3. 避免重複過往發現,改打沒被覆蓋的維度

# 輸出格式(strict JSON,不要 markdown fence、不要前後綴文字)
{{
  "cases": [
    {{
      "input": {{ ... }},
      "target_invariant": "<invariant name>",
      "hypothesis": "<為何認為會失敗>"
    }},
    ...
  ]
}}
"""

    def _parse(self, text: str, n: int) -> list[dict]:
        # 容錯:去 markdown fence、找第一個 { ... } block
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            # 去掉開頭 "json\n"
            if text.lstrip().lower().startswith("json"):
                text = text.split("\n", 1)[1] if "\n" in text else text
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 找最外層 {}
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < 0:
                logger.warning("LLM 回應無法解析為 JSON: %s...", text[:200])
                return []
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                logger.warning("LLM 回應 JSON 解析失敗: %s", exc)
                return []

        cases = data.get("cases", []) if isinstance(data, dict) else []
        # 只取 input dict;hypothesis 之後可餵 reporter
        out: list[dict] = []
        for c in cases[:n]:
            if isinstance(c, dict) and "input" in c and isinstance(c["input"], dict):
                payload = dict(c["input"])
                payload["__hypothesis"] = c.get("hypothesis")
                payload["__target_invariant"] = c.get("target_invariant")
                out.append(payload)
        return out


class OfflineClaudeAttacker(Attacker):
    """離線 Claude 攻擊器:讀預先生成的 case 庫(由 Claude 對 invariants 思考產出)。

    與 LLMAttacker 的差別:不打 API,case 來自 evals/strategies/offline_claude_cases.py,
    人類(或 Claude in chat)離線寫好;適合無 API key 的展示與 CI 重跑。
    """

    name = "offline-claude"

    def generate(
        self, target, n: int, prior_findings: list[dict] | None = None
    ) -> list[dict]:
        from evals.strategies.offline_claude_cases import CASES_BY_TARGET

        bank = CASES_BY_TARGET.get(target.name, [])
        if not bank:
            logger.warning(
                "OfflineClaudeAttacker: target=%s 無 offline case 庫,回空 list",
                target.name,
            )
            return []
        out: list[dict] = []
        for entry in bank[:n]:
            payload = dict(entry["input"])
            payload["__hypothesis"] = entry.get("__hypothesis")
            payload["__target_invariant"] = entry.get("__target_invariant")
            out.append(payload)
        return out


def build_attacker(mode: str = "auto", **kwargs) -> Attacker:
    """根據 mode 與環境決定使用哪個 attacker。"""
    if mode == "heuristic":
        return HeuristicAttacker(**kwargs)
    if mode == "llm":
        return LLMAttacker(**kwargs)
    if mode == "offline-claude":
        return OfflineClaudeAttacker()
    # auto
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return LLMAttacker(**kwargs)
        except RuntimeError as exc:
            logger.info("LLMAttacker 不可用 (%s),降級到 heuristic", exc)
    return HeuristicAttacker()
