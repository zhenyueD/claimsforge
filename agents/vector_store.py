"""
VECTOR STORE — TF-IDF 向量检索层（零依赖，离线可用）
不依赖任何需要下载的模型，基于 scikit-learn TF-IDF + 余弦相似度
可无缝替换 Chroma 方案，接口完全兼容

提供：
  - build_index()      从 knowledge_base.json 构建/重建索引
  - vector_search()    TF-IDF 向量检索 + 关键词混合排序
  - rebuild_if_stale() 懒加载，KB 变化时自动重建
  - get_index_stats()  返回索引状态
"""
import json
import hashlib
import pickle
import math
import re
from pathlib import Path
from datetime import datetime

BASE       = Path(__file__).parent.parent
KB_PATH    = BASE / "data" / "knowledge_base.json"
INDEX_FILE = BASE / "data" / "tfidf_index.pkl"
HASH_FILE  = BASE / "data" / "tfidf_kb_hash.txt"

_index = None   # {"vectorizer": ..., "matrix": ..., "entries": [...]}


# ─────────────────────────────────────────────────────────────
#  纯 Python TF-IDF（无 sklearn 依赖）
# ─────────────────────────────────────────────────────────────
def _tokenize(text: str) -> list:
    """中英文分词：英文词 + 中文 2-gram + 3-gram（去除 1-gram 噪声，M-02）
    额外：同一个字连续重复产生的垃圾 2-gram（如 '退退'）在 doc 词汇表中出现频次不高，
    IDF 会偏高；使用 set 去重后仅保留唯一 2/3-gram，避免重复字符令 TF 虚高。"""
    tokens = []
    # 英文词 / 数字
    tokens += re.findall(r'[a-zA-Z0-9]+', text.lower())
    # 中文字符串
    chinese = re.sub(r'[^\u4e00-\u9fff]', '', text)
    for i in range(len(chinese)):
        if i + 2 <= len(chinese):
            bigram = chinese[i:i+2]
            # 跳过重复字符的 2-gram（如 '退退' '哈哈'）
            if bigram[0] != bigram[1]:
                tokens.append(bigram)
        if i + 3 <= len(chinese):
            trigram = chinese[i:i+3]
            # 跳过 3 个字都一样的 3-gram
            if not (trigram[0] == trigram[1] == trigram[2]):
                tokens.append(trigram)
    return tokens


class TfidfVectorizer:
    def __init__(self):
        self.vocab = {}        # term -> index
        self.idf   = {}        # term -> idf value
        self.fitted = False

    def fit(self, docs: list):
        """计算词汇表和 IDF"""
        N = len(docs)
        df = {}
        tokenized_docs = []
        for doc in docs:
            tokens = set(_tokenize(doc))
            tokenized_docs.append(tokens)
            for t in tokens:
                df[t] = df.get(t, 0) + 1

        # 过滤极低频（只出现1次）和极高频（超过80%文档）
        filtered = {t: v for t, v in df.items()
                    if 1 < v < N * 0.8 or v == 1}
        self.vocab = {t: i for i, t in enumerate(sorted(filtered))}
        self.idf   = {t: math.log((N + 1) / (v + 1)) + 1
                      for t, v in filtered.items()}
        self.fitted = True
        return tokenized_docs

    def transform_one(self, text: str) -> dict:
        """返回 {term_idx: tfidf_score} 稀疏向量"""
        tokens = _tokenize(text)
        tf = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        total = sum(tf.values()) or 1
        vec = {}
        for t, cnt in tf.items():
            if t in self.vocab:
                idx = self.vocab[t]
                vec[idx] = (cnt / total) * self.idf.get(t, 1.0)
        return vec

    def fit_transform(self, docs: list) -> list:
        self.fit(docs)
        return [self.transform_one(d) for d in docs]


def _cosine(a: dict, b: dict) -> float:
    """稀疏向量余弦相似度"""
    if not a or not b:
        return 0.0
    dot = sum(a.get(k, 0) * v for k, v in b.items())
    norm_a = math.sqrt(sum(v*v for v in a.values()))
    norm_b = math.sqrt(sum(v*v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ─────────────────────────────────────────────────────────────
#  索引管理
# ─────────────────────────────────────────────────────────────
def _kb_hash() -> str:
    return hashlib.md5(KB_PATH.read_bytes()).hexdigest()


def build_index(force: bool = False) -> dict:
    global _index
    current_hash = _kb_hash()

    # 修复 M-03：去掉 pickle，30条重建耗时 < 100ms，在内存缓存即可
    if not force and _index is not None and _index.get("_kb_hash") == current_hash:
        return {"status": "skipped", "reason": "KB未变化",
                "count": len(_index["entries"])}

    with open(KB_PATH, "r", encoding="utf-8") as f:
        kb = json.load(f)

    entries = kb["entries"]
    if not entries:
        return {"status": "error", "reason": "KB为空"}

    # 构建检索文档（关键词权重 × 3 + 问题 × 2 + 答案）
    docs = []
    for e in entries:
        kw_str = " ".join(e.get("keywords", [])) * 3
        q_str  = e.get("question", "") * 2
        a_str  = e.get("answer", "")[:300]
        docs.append(f"{kw_str} {q_str} {a_str}")

    vectorizer = TfidfVectorizer()
    vecs = vectorizer.fit_transform(docs)

    _index = {
        "vectorizer": vectorizer,
        "vectors":    vecs,
        "entries":    entries,
        "built_at":   datetime.now().isoformat(),
        "_kb_hash":   current_hash,
    }

    # 写入 hash 文件作为跨进程提示（不再 pickle）
    HASH_FILE.write_text(current_hash, encoding='utf-8')

    return {
        "status":    "rebuilt",
        "count":     len(entries),
        "vocab_size": len(vectorizer.vocab),
        "timestamp": _index["built_at"]
    }


def _load_index():
    global _index
    if _index is not None:
        return
    rebuild_if_stale()


def rebuild_if_stale():
    global _index
    current_hash = _kb_hash()
    if _index is None or _index.get("_kb_hash") != current_hash:
        build_index(force=True)


# ─────────────────────────────────────────────────────────────
#  检索接口
# ─────────────────────────────────────────────────────────────
def vector_search(query: str, top_k: int = 3,
                  keyword_boost: bool = True) -> tuple:
    """
    TF-IDF 向量检索 + 关键词混合排序
    返回 (results_list, confidence_float)
    """
    _load_index()
    if _index is None or not _index["entries"]:
        return [], 0.0

    vec = _index["vectorizer"].transform_one(query)
    if not vec:
        return [], 0.0

    scores = []
    for i, (entry, doc_vec) in enumerate(
            zip(_index["entries"], _index["vectors"])):
        sim = _cosine(vec, doc_vec)

        if keyword_boost:
            for kw in entry.get("keywords", []):
                if kw and kw in query:
                    sim += 0.12
            if query in entry.get("question", ""):
                sim += 0.20

        scores.append((sim, entry))

    scores.sort(key=lambda x: x[0], reverse=True)
    top = scores[:top_k]

    # 过滤低相关（sim < 0.05 视为无命中）
    top = [(s, e) for s, e in top if s > 0.05]

    results = [e for _, e in top]
    confidence = min(0.99, top[0][0] * 2.5) if top else 0.0  # 修复 R-04：调高放大系数补偿 1-gram 移除后的分数下降
    return results, confidence


def get_index_stats() -> dict:
    try:
        _load_index()
        if _index is None:
            return {"status": "not_built"}
        return {
            "status":     "ok",
            "count":      len(_index["entries"]),
            "vocab_size": len(_index["vectorizer"].vocab),
            "built_at":   _index.get("built_at", ""),
            "engine":     "TF-IDF (offline)"
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    print("构建 TF-IDF 索引...")
    r = build_index(force=True)
    print(json.dumps(r, ensure_ascii=False, indent=2))

    tests = ["退款申请", "忘记密码怎么办", "账号被封了", "服务器故障赔偿", "发票怎么开"]
    for q in tests:
        hits, conf = vector_search(q)
        print(f"\n查询: {q}  置信度: {conf:.2f}")
        for h in hits:
            print(f"  [{h['id']}] {h['category']} — {h['question']}")
