"""ESG-XK PP-OCR Helper（在外部 PP-OCR runtime 的 python.exe 中运行）。

协议：JSON Lines
  启动完成 → {"event":"ready", ...}      启动失败 → {"event":"fatal","error":...}
  请求     ← {"id":1,"image_path":"..."} 或 {"cmd":"shutdown"}
  响应     → {"id":1,"status":"OK","text":...,"lines":[...],"confidence":0.97,"duration_ms":123}
只读取传入的图片文件，不写任何文件。第三方库的控制台输出被重定向到 stderr，保证协议通道干净。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback


def main() -> int:
    proto_fd = os.dup(1)
    os.dup2(2, 1)  # 库的 print / C 层输出 → stderr
    proto = os.fdopen(proto_fd, "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr

    def send(obj: dict) -> None:
        proto.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proto.flush()

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--model-root", required=True)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    try:
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        import numpy as np
        import cv2
        from importlib import metadata
        from paddleocr import PaddleOCR

        root = args.model_root
        t0 = time.time()
        ocr = PaddleOCR(
            text_detection_model_name="PP-OCRv6_small_det",
            text_detection_model_dir=os.path.join(root, "detection"),
            text_recognition_model_name="PP-OCRv6_small_rec",
            text_recognition_model_dir=os.path.join(root, "recognition"),
            textline_orientation_model_name="PP-LCNet_x1_0_textline_ori",
            textline_orientation_model_dir=os.path.join(root, "textline_orientation"),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=True,
            device="cpu",
            enable_mkldnn=False,
            cpu_threads=max(1, args.threads),
        )
        send({"event": "ready", "load_seconds": round(time.time() - t0, 2),
              "paddleocr_version": metadata.version("paddleocr"),
              "paddlepaddle_version": metadata.version("paddlepaddle"),
              "python": sys.version.split()[0]})
    except BaseException as exc:  # noqa: BLE001
        send({"event": "fatal", "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})
        return 2

    def to_py(v):
        if hasattr(v, "tolist"):
            return v.tolist()
        return v

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        if req.get("cmd") == "shutdown":
            send({"event": "bye"})
            return 0
        rid = req.get("id")
        t0 = time.time()
        if req.get("fault") == "hang":          # 仅故障注入测试：模拟 OCR 永久无响应
            while True:
                time.sleep(3600)
        if req.get("fault") == "crash":         # 仅故障注入测试：模拟 OCR 子进程崩溃
            os._exit(3)
        try:
            with open(req["image_path"], "rb") as fh:
                buf = np.frombuffer(fh.read(), dtype=np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("图片解码失败")
            lines = []
            for res in ocr.predict(img):
                value = res.json if hasattr(res, "json") else res
                value = value() if callable(value) else value
                data = value.get("res", value) if isinstance(value, dict) else {}
                texts = list(to_py(data.get("rec_texts", [])) or [])
                scores = list(to_py(data.get("rec_scores", [])) or [])
                polys = list(to_py(data.get("rec_polys", data.get("dt_polys", []))) or [])
                for i, t in enumerate(texts):
                    lines.append({"text": str(t),
                                  "confidence": float(scores[i]) if i < len(scores) else None,
                                  "polygon": to_py(polys[i]) if i < len(polys) else None})
            confs = [ln["confidence"] for ln in lines if ln["confidence"] is not None]
            send({"id": rid, "status": "OK", "text": "\n".join(ln["text"] for ln in lines), "lines": lines,
                  "confidence": (sum(confs) / len(confs)) if confs else None,
                  "duration_ms": int((time.time() - t0) * 1000)})
        except BaseException as exc:  # noqa: BLE001
            send({"id": rid, "status": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                  "duration_ms": int((time.time() - t0) * 1000)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
