from __future__ import annotations

import shutil

import pytest

from digest.config import CONFIG_DIR, ConfigError, load_config


def test_security_is_limited_per_day(config):
    assert config.diversity.topic_limits == {"安全": 2}
    assert config.mark_exploited
    assert "security" in config.section_by_key
    # 安全研究的来源一天更新一次，只放早间版；晚间版的安全新闻和早间版合计限量
    assert "security" not in config.evening.sections


@pytest.mark.parametrize("limits", ["{安保: 2}", "{安全: -1}"])
def test_bad_topic_limits(tmp_path, limits):
    shutil.copytree(CONFIG_DIR, tmp_path, dirs_exist_ok=True)
    interests = tmp_path / "interests.yaml"
    text = interests.read_text(encoding="utf-8")
    interests.write_text(text.replace("  topic_limits:\n    安全: 2", f"  topic_limits: {limits}"), encoding="utf-8")
    with pytest.raises(ConfigError, match="topic_limits"):
        load_config(tmp_path)
