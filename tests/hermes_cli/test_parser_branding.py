"""CLI parser branding smoke tests."""

from hermes_cli._parser import build_top_level_parser


def test_top_level_help_uses_reuben_as_primary_command():
    parser, _subparsers, _chat_parser = build_top_level_parser()

    help_text = parser.format_help()

    assert help_text.startswith("usage: reuben ")
    assert "Reuben Agent - AI assistant" in help_text
    assert "reuben setup" in help_text
    assert "hermes setup" not in help_text
