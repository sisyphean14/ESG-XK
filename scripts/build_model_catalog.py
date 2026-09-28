"""生成模型下载目录（src/esgxk/resources/models/catalog.json）：从官方来源查询并固定每个文件的大小与 SHA-256。

开发期执行一次；程序运行时只读取生成的 catalog.json，下载后按其中的 SHA-256 校验（任何镜像被篡改都会被拒绝）。
"""
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "esgxk" / "resources" / "models" / "catalog.json"
UA = {"User-Agent": "ESG-XK/1.1 catalog-builder"}
LLAMA_TAG = "b11228"
PY_VER = "3.12.10"
PADDLE_VER = "3.3.1"
PADDLEOCR_VER = "3.7.0"


def get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def ms_files(repo):
    d = json.loads(get(f"https://www.modelscope.cn/api/v1/models/{repo}/repo/files?Recursive=true"))
    return {f["Path"]: (f["Size"], f["Sha256"]) for f in d["Data"]["Files"]}


def model_sources(repo, path):
    return [f"https://www.modelscope.cn/models/{repo}/resolve/master/{path}",
            f"https://hf-mirror.com/{repo}/resolve/main/{path}",
            f"https://huggingface.co/{repo}/resolve/main/{path}"]


def hf_file(repo, path):
    files = ms_files(repo)
    size, sha = files[path]
    return {"name": path, "size": size, "sha256": sha, "sources": model_sources(repo, path)}


def gh_assets():
    rel = json.loads(get(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{LLAMA_TAG}"))
    return {a["name"]: (a["size"], a["digest"].split(":", 1)[1], a["browser_download_url"]) for a in rel["assets"]}


def main():
    assets = gh_assets()

    def gh(name):
        size, sha, url = assets[name]
        return {"name": name, "size": size, "sha256": sha, "sources": [url], "extract": True}

    items = []
    items.append({"id": "llama-cpu", "kind": "llama_server", "title": "llama.cpp 推理服务（CPU 版）",
                  "desc": "运行 Embedding / Reranker / LLM 的本地推理服务。适用于没有 NVIDIA 显卡的电脑。",
                  "subdir": "llama.cpp-cpu", "entry": "llama-server.exe", "requires": {"gpu": None},
                  "files": [gh(f"llama-{LLAMA_TAG}-bin-win-cpu-x64.zip")]})
    items.append({"id": "llama-cuda", "kind": "llama_server", "title": "llama.cpp 推理服务（NVIDIA GPU 版 CUDA 12.4）",
                  "desc": "使用 NVIDIA 显卡加速，速度显著快于 CPU 版。需要较新的 NVIDIA 驱动。",
                  "subdir": "llama.cpp-cuda", "entry": "llama-server.exe", "requires": {"gpu": "nvidia"},
                  "files": [gh(f"llama-{LLAMA_TAG}-bin-win-cuda-12.4-x64.zip"),
                            gh("cudart-llama-bin-win-cuda-12.4-x64.zip")]})
    items.append({"id": "embed-qwen3-0.6b", "kind": "embedding_model", "title": "Embedding：Qwen3-Embedding-0.6B（Q8_0）",
                  "desc": "中 / 英 / 印尼语跨语言语义检索向量模型。", "subdir": "embedding",
                  "entry": "Qwen3-Embedding-0.6B-Q8_0.gguf", "requires": {"ram_gb": 8},
                  "files": [hf_file("Qwen/Qwen3-Embedding-0.6B-GGUF", "Qwen3-Embedding-0.6B-Q8_0.gguf")]})
    items.append({"id": "rerank-qwen3-0.6b", "kind": "reranker_model", "title": "Reranker：Qwen3-Reranker-0.6B（Q8_0）",
                  "desc": "检索结果重排序，提高问答与检索准确性。", "subdir": "reranker",
                  "entry": "qwen3-reranker-0.6b-q8_0.gguf", "requires": {"ram_gb": 8},
                  "files": [hf_file("ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF", "qwen3-reranker-0.6b-q8_0.gguf")]})
    items.append({"id": "llm-qwen3-1.7b", "kind": "llm_model", "title": "本地大模型：Qwen3-1.7B（Q8_0，轻量）",
                  "desc": "适合 8–16 GB 内存、无独立显卡的电脑；回答速度较快。", "subdir": "llm",
                  "entry": "Qwen3-1.7B-Q8_0.gguf", "requires": {"ram_gb": 8},
                  "files": [hf_file("Qwen/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q8_0.gguf")]})
    items.append({"id": "llm-qwen3-4b", "kind": "llm_model", "title": "本地大模型：Qwen3-4B（Q4_K_M，推荐）",
                  "desc": "回答质量更好；建议 16 GB 内存以上，或有 6 GB 以上显存的 NVIDIA 显卡。", "subdir": "llm",
                  "entry": "Qwen3-4B-Q4_K_M.gguf", "requires": {"ram_gb": 16},
                  "files": [hf_file("Qwen/Qwen3-4B-GGUF", "Qwen3-4B-Q4_K_M.gguf")]})
    ocr_files = []
    for repo, sub in (("PaddlePaddle/PP-OCRv5_mobile_det", "PP-OCRv5_mobile_det"),
                      ("PaddlePaddle/PP-OCRv5_mobile_rec", "PP-OCRv5_mobile_rec"),
                      ("PaddlePaddle/PP-LCNet_x1_0_textline_ori", "PP-LCNet_x1_0_textline_ori")):
        files = ms_files(repo)
        for fn in ("inference.json", "inference.pdiparams", "inference.yml", "config.json"):
            if fn in files:
                size, sha = files[fn]
                ocr_files.append({"name": f"{sub}/{fn}", "size": size, "sha256": sha,
                                  "sources": model_sources(repo, fn)})
    items.append({"id": "ocr-models-ppocrv5", "kind": "ocr_model_root", "title": "OCR 模型：PP-OCRv5 mobile（检测 + 识别 + 方向）",
                  "desc": "中文 / English / 印尼语等拉丁文字识别模型（需配合 PP-OCR 运行时）。", "subdir": "ppocr-models",
                  "entry": ".", "requires": {}, "files": ocr_files})
    # PP-OCR 运行时：Python 嵌入包 + pip wheel（固定 SHA-256），paddle 依赖由 pip 从镜像安装并校验
    py_name = f"python-{PY_VER}-embed-amd64.zip"
    py_bytes = get(f"https://registry.npmmirror.com/-/binary/python/{PY_VER}/{py_name}", timeout=300)
    py_sha = hashlib.sha256(py_bytes).hexdigest()
    idx = get("https://pypi.tuna.tsinghua.edu.cn/simple/pip/").decode()
    wheels = re.findall(r'href="([^"]*?/(pip-(\d+)\.(\d+)(?:\.(\d+))?-py3-none-any\.whl))#sha256=([0-9a-f]{64})"', idx)
    wheels.sort(key=lambda w: (int(w[2]), int(w[3]), int(w[4] or 0)))
    href, pip_name, *_, pip_sha = wheels[-1]
    pip_url = href if href.startswith("http") else "https://pypi.tuna.tsinghua.edu.cn" + href if href.startswith("/") \
        else "https://pypi.tuna.tsinghua.edu.cn/simple/pip/" + href
    pip_size = int(urllib.request.urlopen(urllib.request.Request(pip_url, method="HEAD", headers=UA), timeout=60)
                   .headers.get("Content-Length") or 0)
    items.append({"id": "ppocr-runtime", "kind": "ocr_python", "title": "PP-OCR 运行时（Python 3.12 + PaddlePaddle + PaddleOCR，CPU）",
                  "desc": f"离线 OCR 引擎运行环境：嵌入式 Python {PY_VER} + paddlepaddle {PADDLE_VER} + paddleocr {PADDLEOCR_VER}。"
                          "安装过程需联网下载约 0.5–1 GB 依赖，耗时较长。", "subdir": "ppocr-runtime", "entry": "python.exe",
                  "requires": {"ram_gb": 8},
                  "files": [{"name": py_name, "size": len(py_bytes), "sha256": py_sha, "extract": True,
                             "sources": [f"https://registry.npmmirror.com/-/binary/python/{PY_VER}/{py_name}",
                                         f"https://www.python.org/ftp/python/{PY_VER}/{py_name}"]},
                            {"name": pip_name, "size": pip_size, "sha256": pip_sha,
                             "sources": [pip_url, f"https://files.pythonhosted.org/packages/py3/p/pip/{pip_name}"]}],
                  "pip": {"packages": [f"paddlepaddle=={PADDLE_VER}", f"paddleocr=={PADDLEOCR_VER}"],
                          "indexes": ["https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.aliyun.com/pypi/simple",
                                      "https://pypi.org/simple"],
                          "estimated_bytes": 900_000_000}})
    catalog = {"catalog_version": 1, "generated_for": "ESG-XK 1.1.0", "llama_cpp_tag": LLAMA_TAG, "items": items}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    for it in items:
        print(it["id"], sum(f["size"] for f in it["files"]), len(it["files"]))


if __name__ == "__main__":
    sys.exit(main())
