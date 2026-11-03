"""画面認識(Vision)。差し替え可能なRecognizerインターフェース。"""
from __future__ import annotations

import base64
import json
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class BoundingBox:
    x: float
    y: float
    width: float
    height: float
    confidence: float = 1.0

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


class Recognizer(ABC):
    """画面上の要素を認識するインターフェース。実装を差し替え可能にする。"""

    @abstractmethod
    def locate(self, image_path: Path, description: str) -> Optional[BoundingBox]:
        ...


class ClaudeVisionRecognizer(Recognizer):
    """Claude Vision APIでバウンディングボックスを取得する実装。"""

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def locate(self, image_path: Path, description: str) -> Optional[BoundingBox]:
        image_bytes = Path(image_path).read_bytes()
        image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        prompt = (
            f"画像内の「{description}」に該当するUI要素を探してください。"
            "見つかった場合はピクセル座標で"
            '{"x": int, "y": int, "width": int, "height": int, "confidence": float(0-1)} '
            "のJSONのみを返してください。見つからない場合は null のみを返してください。"
        )

        response = self.client.messages.create(
            model=self.model,
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": image_b64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )

        text = response.content[0].text.strip()
        if text.lower() == "null":
            return None

        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        data = json.loads(text)
        return BoundingBox(
            x=data["x"], y=data["y"], width=data["width"], height=data["height"],
            confidence=data.get("confidence", 1.0),
        )


class TemplateMatchRecognizer(Recognizer):
    """OpenCVテンプレートマッチングによる補助実装。要素名→テンプレート画像パスの辞書を使う。"""

    def __init__(self, templates: dict[str, Path], threshold: float = 0.85):
        self.templates = templates
        self.threshold = threshold

    def locate(self, image_path: Path, description: str) -> Optional[BoundingBox]:
        import cv2

        template_path = self.templates.get(description)
        if template_path is None:
            return None

        screen = cv2.imread(str(image_path))
        template = cv2.imread(str(template_path))
        if screen is None or template is None:
            return None

        result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val < self.threshold:
            return None

        h, w = template.shape[:2]
        return BoundingBox(x=max_loc[0], y=max_loc[1], width=w, height=h, confidence=float(max_val))


class DummyRecognizer(Recognizer):
    """APIキー未設定時のダミー実装。常にNone(未検出)を返す。"""

    def locate(self, image_path: Path, description: str) -> Optional[BoundingBox]:
        return None


def locate_with_retry(
    recognizer: Recognizer,
    capture_fn,
    description: str,
    max_retries: int = 3,
    retry_wait_range: tuple[float, float] = (0.8, 2.0),
    min_confidence: float = 0.5,
    rng: Optional[random.Random] = None,
) -> Optional[BoundingBox]:
    """スクショ取得→認識を、見つかるまで(または上限まで)繰り返す。"""
    rng = rng or random.Random()

    for attempt in range(max_retries):
        image_path = capture_fn()
        box = recognizer.locate(image_path, description)

        if box is not None and box.confidence >= min_confidence:
            return box

        if attempt < max_retries - 1:
            time.sleep(rng.uniform(*retry_wait_range))

    return None


def draw_debug_image(image_path: Path, box: Optional[BoundingBox], output_path: Path) -> Path:
    """認識結果のバウンディングボックスを描画してデバッグ用に保存する。"""
    import cv2

    image = cv2.imread(str(image_path))
    if box is not None:
        pt1 = (int(box.x), int(box.y))
        pt2 = (int(box.x + box.width), int(box.y + box.height))
        cv2.rectangle(image, pt1, pt2, (0, 0, 255), 2)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), image)
    return output_path
