"""The command line parses without touching a network."""

import pytest

from kokoah_subnet.cli import build_parser


def test_info_defaults_to_the_localnet() -> None:
    args = build_parser().parse_args(["info"])
    assert args.command == "info"
    assert args.network == "local"
    assert args.json is False


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
