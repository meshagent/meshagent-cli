from meshagent.cli.preamble_rules import DEFAULT_PREAMBLE_RULE


def test_default_preamble_rule_requests_grouped_commentary() -> None:
    assert "a related group of tool calls" in DEFAULT_PREAMBLE_RULE
    assert "Do not send a separate commentary message" in DEFAULT_PREAMBLE_RULE
    assert "obvious or trivial single-step tool calls" in DEFAULT_PREAMBLE_RULE
