import struct
import sys
import types


def test_pipecat_smart_turn_uses_local_analyzer(monkeypatch):
    from tools import semantic_turn

    calls = {}

    class FakeAnalyzer:
        def __init__(self, sample_rate=16000):
            calls["sample_rate"] = sample_rate

        def _predict_endpoint(self, audio):
            calls["samples"] = len(audio)
            return {"prediction": 1, "probability": 0.91}

    fake_module = types.ModuleType("pipecat.audio.turn.smart_turn")
    fake_module.LocalSmartTurnAnalyzerV3 = FakeAnalyzer
    monkeypatch.setitem(sys.modules, "pipecat.audio.turn.smart_turn", fake_module)
    semantic_turn._smart_turn_analyzer.cache_clear()

    result = semantic_turn.pipecat_smart_turn_complete([struct.pack("<3f", 0.1, 0.2, 0.3)], 16000)

    assert result is True
    assert calls == {"sample_rate": 16000, "samples": 3}
