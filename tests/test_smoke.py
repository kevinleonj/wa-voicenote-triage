from wa_voicenote import __version__


def test_version_is_set() -> None:
    assert isinstance(__version__, str)
    assert len(__version__) > 0
