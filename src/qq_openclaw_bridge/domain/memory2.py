import hashlib
import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


VECTOR_DIMENSIONS = 96
_BULLET_PREFIX = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_WORD_PATTERN = re.compile(r"[a-zA-Z0-9_+#.-]+|[\u4e00-\u9fff]+")
_PUNCTUATION = "。！？!?，,；;：:、. "
_STOPWORDS = {
    "用户",
    "一个",
    "这个",
    "那个",
    "需要",
    "记得",
    "我的",
    "我们",
    "你们",
    "他们",
    "然后",
    "之前",
}
_SEMANTIC_GROUPS = (
    ("忌口", "不吃", "过敏", "饮食"),
    ("喜欢", "偏好", "爱好"),
    ("项目", "工程", "代码", "开发"),
    ("提醒", "闹钟", "待办", "日程"),
    ("工作", "职业", "公司"),
)


def normalize_memory_text(text: str) -> str:
    return re.sub(r"\s+", "", text).strip(_PUNCTUATION).casefold()


def _features(text: str) -> List[str]:
    features: List[str] = []
    for raw in _WORD_PATTERN.findall(text.casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            features.extend(char for char in raw if char not in _STOPWORDS)
            features.extend(raw[index : index + 2] for index in range(len(raw) - 1))
        elif raw not in _STOPWORDS:
            features.append(raw.strip(".-"))

    feature_set = set(features)
    for group in _SEMANTIC_GROUPS:
        if feature_set.intersection(group) or any(term in text for term in group):
            features.extend(group)
    return [feature for feature in features if feature and feature not in _STOPWORDS]


def extract_keywords(text: str, limit: int = 12) -> Tuple[str, ...]:
    counts = Counter(_features(text))
    ordered = sorted(counts, key=lambda item: (-counts[item], -len(item), item))
    return tuple(ordered[:limit])


def build_feature_vector(
    text: str, dimensions: int = VECTOR_DIMENSIONS
) -> Tuple[float, ...]:
    vector = [0.0] * dimensions
    for feature, count in Counter(_features(text)).items():
        digest = hashlib.sha256(feature.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign * (1.0 + math.log(count))
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude:
        vector = [value / magnitude for value in vector]
    return tuple(round(value, 8) for value in vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def rank_memory_records(
    query: str,
    records: Iterable[Mapping[str, object]],
    limit: int = 6,
) -> List[Dict[str, object]]:
    query_keywords = set(extract_keywords(query))
    query_vector = build_feature_vector(query)
    normalized_query = normalize_memory_text(query)
    ranked: List[Dict[str, object]] = []
    for record in records:
        content = str(record.get("content", ""))
        keywords = set(record.get("keywords") or extract_keywords(content))
        overlap = len(query_keywords.intersection(keywords))
        lexical = overlap / max(1, len(query_keywords))
        normalized_content = normalize_memory_text(content)
        if normalized_query and (
            normalized_query in normalized_content or normalized_content in normalized_query
        ):
            lexical = max(lexical, 1.0)
        raw_vector = record.get("vector") or build_feature_vector(content)
        vector = tuple(float(value) for value in raw_vector)
        semantic = max(0.0, cosine_similarity(query_vector, vector))
        layer_boost = 0.08 if record.get("layer") == "profile" else 0.0
        relevance = min(1.0, (0.62 * lexical) + (0.38 * semantic) + layer_boost)
        item = dict(record)
        item["relevance"] = round(relevance, 6)
        ranked.append(item)
    ranked.sort(key=lambda item: (-float(item["relevance"]), int(item.get("id", 0))))
    return ranked[: max(0, limit)]


def _normalized_rule(rule: str) -> str:
    value = _BULLET_PREFIX.sub("", rule).strip()
    return re.sub(r"[\s%s]+" % re.escape(_PUNCTUATION), "", value).casefold()


def deduplicate_rules(rules: Iterable[str]) -> Tuple[str, ...]:
    seen = set()
    result = []
    for raw_rule in rules:
        rule = _BULLET_PREFIX.sub("", raw_rule).strip()
        if not rule:
            continue
        key = _normalized_rule(rule)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(rule)
    return tuple(result)


def build_memory_context(
    persona_rules: Iterable[str],
    profile: Iterable[str],
    facts: Iterable[str],
    summaries: Iterable[str],
    max_chars: int = 2400,
) -> str:
    sections = (
        ("人格与风格", deduplicate_rules(persona_rules)),
        ("已确认用户档案", tuple(profile)),
        ("相关长期记忆", tuple(facts)),
        ("相关对话摘要", tuple(summaries)),
    )
    lines = ["【仅供回答参考的 E3 记忆上下文】"]
    for title, values in sections:
        if not values:
            continue
        header = "\n%s：" % title
        if len("\n".join(lines)) + len(header) > max_chars:
            break
        lines.append(header.lstrip("\n"))
        for value in values:
            line = "- %s" % str(value).strip()
            candidate = "\n".join(lines + [line])
            if len(candidate) > max_chars:
                break
            lines.append(line)
    context = "\n".join(lines).strip()
    return context[:max_chars]

