from __future__ import annotations

from functools import lru_cache
from typing import Optional


@lru_cache(maxsize=1)
def _smart_turn_analyzer(sample_rate: int):
    try:
        from pipecat.audio.turn.smart_turn import LocalSmartTurnAnalyzerV3
    except Exception:
        try:
            from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
        except Exception:
            try:
                from tools.lazy_deps import ensure

                ensure("voice.semantic_turn", prompt=False)
                from pipecat.audio.turn.smart_turn import LocalSmartTurnAnalyzerV3
            except Exception:
                try:
                    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
                except Exception:
                    return None

    try:
        return LocalSmartTurnAnalyzerV3(sample_rate=sample_rate)
    except TypeError:
        return LocalSmartTurnAnalyzerV3()


def pipecat_smart_turn_complete(chunks: list[bytes], sample_rate: int = 16000) -> Optional[bool]:
    if not chunks:
        return None

    analyzer = _smart_turn_analyzer(sample_rate)
    if analyzer is None:
        return None

    try:
        import numpy as np
    except Exception:
        return None

    audio = np.frombuffer(b"".join(chunks), dtype="<f4")
    if audio.size == 0:
        return None

    predict = getattr(analyzer, "_predict_endpoint", None)
    if predict is None:
        return None

    result = predict(audio)
    if not isinstance(result, dict):
        return None

    prediction = result.get("prediction")
    if prediction is None:
        return None

    return bool(prediction)
