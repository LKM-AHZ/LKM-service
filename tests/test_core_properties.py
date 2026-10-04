"""无需数据库的属性测试：序列化边界、分页与 Redis 键。"""

import json
import uuid

from hypothesis import given
from hypothesis import strategies as st

from core.cache import make_key
from core.common import paginate_offset, paginate_pages, parse_tags
from core.counters import COUNTER_FIELDS, counter_key, parse_counter_key


@given(
    st.lists(
        st.one_of(st.text(max_size=20), st.integers(), st.booleans(), st.none()),
        max_size=20,
    )
)
def test_tags_have_same_meaning_as_list_or_json(values: list[object]) -> None:
    expected = [value for value in values if isinstance(value, str)]
    assert parse_tags(values) == expected
    assert parse_tags(json.dumps(values)) == expected


@given(
    total=st.integers(min_value=0, max_value=10**9),
    size=st.integers(min_value=1, max_value=10**6),
    page=st.integers(min_value=-100, max_value=10**6),
)
def test_pagination_covers_items_without_gap(total: int, size: int, page: int) -> None:
    pages = paginate_pages(total, size)
    if total:
        assert paginate_offset(pages, size) < total
        assert paginate_offset(pages + 1, size) >= total
    else:
        assert pages == 0
    assert paginate_offset(page, size) >= 0
    if page >= 1:
        assert paginate_offset(page + 1, size) - paginate_offset(page, size) == size


@given(
    left=st.text(alphabet="ab%|:", max_size=12),
    middle=st.text(alphabet="ab%|:", max_size=12),
    right=st.text(alphabet="ab%|:", max_size=12),
)
def test_cache_key_segments_cannot_collide(left: str, middle: str, right: str) -> None:
    assert make_key("property", left + "|" + middle, right) != make_key(
        "property", left, middle + "|" + right
    )


@given(field=st.sampled_from(sorted(COUNTER_FIELDS)), object_id=st.uuids())
def test_counter_key_round_trip(field: str, object_id: uuid.UUID) -> None:
    assert parse_counter_key(counter_key(field, object_id)) == (field, object_id)
