"""群术语库：写入/更新、匹配规则、注入格式。"""

from __future__ import annotations

from qq_bot.memory.glossary import format_injection, match_terms


async def test_upsert_then_update(repo) -> None:
    first = await repo.upsert_glossary_term(
        group_id="G100",
        term="我造密码",
        term_key="我造密码",
        definition="一种表达惊讶的口头禅",
    )
    assert first == "created"
    again = await repo.upsert_glossary_term(
        group_id="G100", term="我造密码", term_key="我造密码", definition="改后的释义"
    )
    assert again == "updated"
    rows = await repo.list_glossary("G100")
    assert len(rows) == 1
    assert rows[0]["definition"] == "改后的释义"


async def test_terms_are_group_scoped(repo) -> None:
    await repo.upsert_glossary_term(
        group_id="G100", term="甲", term_key="甲", definition="A"
    )
    assert await repo.list_glossary("G200") == []


def test_match_chinese_substring() -> None:
    terms = [{"term": "我造密码", "term_key": "我造密码", "definition": "x"}]
    assert match_terms(terms, "他又说我造密码了") == terms
    assert match_terms(terms, "完全无关") == []


def test_match_short_ascii_needs_word_boundary() -> None:
    terms = [{"term": "as", "term_key": "as", "definition": "x"}]
    assert match_terms(terms, "as 是什么意思") == terms
    assert match_terms(terms, "asdf 不算") == []


def test_match_longest_first_and_cap() -> None:
    terms = [
        {"term": "ab", "term_key": "ab", "definition": "x"},
        {"term": "abcd", "term_key": "abcd", "definition": "y"},
    ]
    hits = match_terms(terms, "abcd 和 ab 都出现")
    assert [h["term"] for h in hits] == ["abcd", "ab"]


def test_format_injection() -> None:
    text = format_injection([{"term": "梗", "definition": "含义"}])
    assert text.startswith("## 群术语")
    assert "- 「梗」：含义" in text
